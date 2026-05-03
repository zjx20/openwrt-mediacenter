"""DLNA URL 转发代理 - 上游断连时自动 Range 续传

MPD 的 curl 插件配置 proxy 指向本代理（forward proxy 模式）。
当对端（手机/控制点的 HTTP 服务）在中途断连时，本代理会用
Range: bytes=N- 自动重连续传，对 MPD 完全透明，避免 ffmpeg
报 "partial file" 然后停播。
"""

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8081

# RFC 7230 hop-by-hop headers，不能透传
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
})

MAX_RETRIES = 10
RETRY_BACKOFF = 0.5
CHUNK_SIZE = 64 * 1024


class DLNAProxy:
    """HTTP forward proxy，专为 DLNA URL 投屏断点续传场景。"""

    def __init__(self, host: str = PROXY_HOST, port: int = PROXY_PORT):
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info(f"DLNA URL 代理已启动 {self.url}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("DLNA URL 代理已停止")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername")
        try:
            await self._proxy_one(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError) as e:
            logger.debug(f"代理客户端 {peer} 断开: {e}")
        except Exception:
            logger.exception(f"代理处理 {peer} 异常")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _proxy_one(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ):
        request_line = await client_reader.readline()
        if not request_line:
            return
        try:
            method, url, _ = request_line.decode("latin-1").rstrip().split(" ", 2)
        except ValueError:
            logger.warning(f"无法解析请求行: {request_line!r}")
            return

        # CONNECT (HTTPS 隧道) 不支持，DLNA 场景一般是 HTTP
        if method.upper() == "CONNECT":
            client_writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await client_writer.drain()
            return

        request_headers: dict[str, str] = {}
        while True:
            line = await client_reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            try:
                k, _, v = line.decode("latin-1").rstrip().partition(":")
            except UnicodeDecodeError:
                continue
            if k:
                request_headers[k.strip()] = v.strip()

        forward_headers = {
            k: v for k, v in request_headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }
        client_range_start = self._parse_range_start(forward_headers.get("Range"))

        logger.info(f"代理 {method} {url}")

        bytes_sent = 0
        headers_sent = False
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(MAX_RETRIES + 1):
                req_headers = dict(forward_headers)
                if bytes_sent > 0:
                    req_headers["Range"] = f"bytes={client_range_start + bytes_sent}-"

                try:
                    async with session.request(
                        method, url, headers=req_headers, allow_redirects=True
                    ) as upstream:
                        if bytes_sent > 0 and upstream.status != 206:
                            logger.warning(
                                f"代理续传失败 status={upstream.status} "
                                f"(已传 {bytes_sent} 字节)，上游不支持 Range，中止"
                            )
                            return

                        if not headers_sent:
                            await self._send_response_headers(client_writer, upstream)
                            headers_sent = True

                        async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                            client_writer.write(chunk)
                            await client_writer.drain()
                            bytes_sent += len(chunk)

                        return

                except (
                    aiohttp.ClientPayloadError,
                    aiohttp.ClientConnectionError,
                    asyncio.TimeoutError,
                ) as e:
                    if attempt == MAX_RETRIES:
                        logger.warning(
                            f"代理重试 {MAX_RETRIES} 次仍失败 "
                            f"(已传 {bytes_sent} 字节, url={url}): {e}"
                        )
                        return
                    logger.info(
                        f"代理上游断开 (已传 {bytes_sent} 字节, "
                        f"第 {attempt + 1}/{MAX_RETRIES} 次)，续传中: {e}"
                    )
                    await asyncio.sleep(RETRY_BACKOFF)

    async def _send_response_headers(
        self,
        writer: asyncio.StreamWriter,
        upstream: aiohttp.ClientResponse,
    ):
        status_line = f"HTTP/1.1 {upstream.status} {upstream.reason or ''}\r\n"
        writer.write(status_line.encode("latin-1"))

        # aiohttp 已经替我们解了 chunked，下游直接发原始字节，
        # 所以不能再透传 Transfer-Encoding；用 Connection: close 让客户端
        # 在 EOF 时关闭连接
        saw_accept_ranges = False
        for k, v in upstream.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "transfer-encoding":
                continue
            if lk == "accept-ranges":
                saw_accept_ranges = True
            writer.write(f"{k}: {v}\r\n".encode("latin-1"))

        # 上游没声明 Accept-Ranges，但我们的代理本身支持 Range（重试就用它），
        # 主动公告，让 MPD 把流识别为 seekable。仅对正常响应注入。
        if not saw_accept_ranges and upstream.status in (200, 206):
            writer.write(b"Accept-Ranges: bytes\r\n")

        writer.write(b"Connection: close\r\n\r\n")
        await writer.drain()

    @staticmethod
    def _parse_range_start(range_header: str | None) -> int:
        if not range_header:
            return 0
        try:
            unit, _, spec = range_header.partition("=")
            if unit.strip().lower() != "bytes":
                return 0
            start, _, _end = spec.strip().partition("-")
            return int(start) if start else 0
        except (ValueError, AttributeError):
            return 0
