from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import velopack


LATEST_RELEASE_URL = "https://github.com/NeoRrrr/StockDeskPet/releases/latest"
PROJECT_URL = "https://github.com/NeoRrrr/StockDeskPet"
UPDATE_BASE_URL = f"{PROJECT_URL}/releases/latest/download"


class UpdateCheckError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str

    @property
    def has_update(self) -> bool:
        return _version_tuple(self.latest_version) > _version_tuple(self.current_version)


@dataclass(frozen=True, slots=True)
class AutomaticUpdateResult:
    status: Literal["up_to_date", "restart_pending", "manual"]
    current_version: str
    latest_version: str
    release_url: str


def check_download_and_install(
    current_version: str,
    *,
    phase_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int], None] | None = None,
) -> AutomaticUpdateResult:
    """Check, download and stage the newest GitHub release with Velopack.

    A Velopack-managed installation can update itself. Source checkouts and the
    legacy one-file build fall back to a release check and direct users to the
    first Velopack installer once.
    """

    phase = phase_callback or (lambda _message: None)
    progress = progress_callback or (lambda _value: None)
    phase("正在检查 GitHub Releases…")

    # GitHub's public API is limited to 60 anonymous requests per shared IP.
    # The latest-release download endpoint is a static redirect and therefore
    # keeps one-click updates working without embedding a user or app token.
    source = velopack.HttpSource(UPDATE_BASE_URL)
    try:
        manager = velopack.UpdateManager(source)
        installed_version = manager.get_current_version().lstrip("vV")
    except RuntimeError:
        info = check_latest_release(current_version)
        return AutomaticUpdateResult(
            status="manual" if info.has_update else "up_to_date",
            current_version=info.current_version,
            latest_version=info.latest_version,
            release_url=info.release_url,
        )

    try:
        update = manager.check_for_updates()
        if update is None:
            return AutomaticUpdateResult(
                status="up_to_date",
                current_version=installed_version,
                latest_version=installed_version,
                release_url=LATEST_RELEASE_URL,
            )

        latest_version = update.TargetFullRelease.Version.lstrip("vV")
        phase(f"发现 v{latest_version}，正在下载…")
        manager.download_updates(update, lambda value: progress(int(value)))
        phase("下载完成，正在准备替换并重启…")
        manager.wait_exit_then_apply_updates(
            update,
            silent=True,
            restart=True,
        )
    except Exception as exc:
        raise UpdateCheckError(f"自动更新失败：{exc}") from exc

    return AutomaticUpdateResult(
        status="restart_pending",
        current_version=installed_version,
        latest_version=latest_version,
        release_url=LATEST_RELEASE_URL,
    )


def check_latest_release(current_version: str, timeout: float = 8.0) -> UpdateInfo:
    request = Request(
        LATEST_RELEASE_URL,
        method="HEAD",
        headers={"User-Agent": f"StockDeskPet/{current_version}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            release_url = response.geturl()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"检查更新失败：{exc}") from exc

    path = unquote(urlparse(release_url).path).rstrip("/")
    match = re.search(r"/releases/tag/([^/]+)$", path)
    if match is None:
        raise UpdateCheckError("暂未找到可用的 GitHub Release。")
    latest_version = match.group(1).lstrip("vV")
    return UpdateInfo(
        current_version=current_version.lstrip("vV"),
        latest_version=latest_version,
        release_url=release_url,
    )


def _version_tuple(version: str) -> tuple[int, int, int]:
    numbers = [int(value) for value in re.findall(r"\d+", version)[:3]]
    return tuple((numbers + [0, 0, 0])[:3])  # type: ignore[return-value]
