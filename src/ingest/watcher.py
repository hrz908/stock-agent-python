"""文件监听器：使用 watchdog 监控目录变化，2s 去抖后触发处理。"""
import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ingest.config import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 2.0


class IngestHandler(FileSystemEventHandler):
    """
    watchdog 事件处理器，带 2s 去抖。
    
    同路径事件聚合后，等文件写完再处理。
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        process_callback: Callable[[str], Coroutine],
        delete_callback: Callable[[str], Coroutine],
    ):
        super().__init__()
        self.loop = loop
        self.process_callback = process_callback
        self.delete_callback = delete_callback
        self._pending: dict[str, float] = {}  # path -> last_event_time
        self._timers: dict[str, threading.Timer | None] = {}
        self._lock = threading.Lock()

    def _is_supported(self, path: str) -> bool:
        """检查文件扩展名是否支持"""
        ext = Path(path).suffix.lower()
        return ext in SUPPORTED_EXTENSIONS

    def _schedule(self, path: str):
        """去抖调度：取消之前的 timer，重新计时"""
        with self._lock:
            now = time.time()
            self._pending[path] = now

            if path in self._timers and self._timers[path] is not None:
                self._timers[path].cancel()

            timer = threading.Timer(DEBOUNCE_SECONDS, self._fire, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str):
        """去抖到期，触发处理"""
        with self._lock:
            self._timers.pop(path, None)
            self._pending.pop(path, None)

        # 检查文件是否还存在（可能已被删除）
        if Path(path).exists():
            future = asyncio.run_coroutine_threadsafe(
                self.process_callback(path), self.loop
            )
            try:
                future.result(timeout=600)
            except Exception as e:
                logger.error("处理文件回调异常: %s - %s", path, e)

    def _fire_delete(self, path: str):
        """文件删除，触发清理"""
        with self._lock:
            self._timers.pop(path, None)
            self._pending.pop(path, None)

        future = asyncio.run_coroutine_threadsafe(
            self.delete_callback(path), self.loop
        )
        try:
            future.result(timeout=60)
        except Exception as e:
            logger.error("删除文件回调异常: %s - %s", path, e)

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        if self._is_supported(event.src_path):
            logger.info("检测到新文件: %s", event.src_path)
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        if self._is_supported(event.src_path):
            logger.info("检测到文件修改: %s", event.src_path)
            self._schedule(event.src_path)

    def on_deleted(self, event: FileSystemEvent):
        if event.is_directory:
            return
        if self._is_supported(event.src_path):
            logger.info("检测到文件删除: %s", event.src_path)
            # 删除不需要去抖，但给一个短延时避免误触发
            with self._lock:
                if event.src_path in self._timers and self._timers[event.src_path] is not None:
                    self._timers[event.src_path].cancel()
                    self._timers.pop(event.src_path, None)
                self._pending.pop(event.src_path, None)

            timer = threading.Timer(DEBOUNCE_SECONDS, self._fire_delete, args=(event.src_path,))
            timer.daemon = True
            timer.start()

    def on_moved(self, event: FileSystemEvent):
        """文件移动：旧路径当删除，新路径当创建"""
        if event.is_directory:
            return
        if self._is_supported(event.src_path):
            self._fire_delete(event.src_path)
        if hasattr(event, "dest_path") and self._is_supported(event.dest_path):
            self._schedule(event.dest_path)


def start_watcher(
    watch_dir: str,
    loop: asyncio.AbstractEventLoop,
    process_callback: Callable[[str], Coroutine],
    delete_callback: Callable[[str], Coroutine],
) -> Observer:
    """
    启动文件监听。
    
    Args:
        watch_dir: 监听目录
        loop: asyncio 事件循环
        process_callback: 文件创建/修改时的异步回调
        delete_callback: 文件删除时的异步回调
    
    Returns:
        Observer 实例（调用 .stop() 和 .join() 来停止）
    """
    Path(watch_dir).mkdir(parents=True, exist_ok=True)

    handler = IngestHandler(loop, process_callback, delete_callback)
    observer = Observer()
    observer.schedule(handler, watch_dir, recursive=True)
    observer.daemon = True
    observer.start()

    logger.info("文件监听已启动: %s (recursive=True, debounce=%.1fs)", watch_dir, DEBOUNCE_SECONDS)
    return observer

