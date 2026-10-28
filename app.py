from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import velopack
from PySide6.QtCore import QLockFile
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from stock_pet import __version__
from stock_pet.diagnostics import UiStallMonitor
from stock_pet.resources import asset_path
from stock_pet.ui import StockPetWidget


def main() -> int:
    # Velopack must process install/update hooks before normal application startup.
    velopack.App().run()

    app = QApplication(sys.argv)
    app.setApplicationName("StockDeskPet")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("StockDeskPet")
    app.setQuitOnLastWindowClosed(False)
    app_icon = QIcon(str(asset_path("app_icon.png")))
    app.setWindowIcon(app_icon)

    lock_path = Path(tempfile.gettempdir()) / "stock-desk-pet.lock"
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(100):
        QMessageBox.information(None, "股票桌宠", "股票桌宠已经在运行。")
        return 0

    stall_monitor = UiStallMonitor(app)
    app.aboutToQuit.connect(stall_monitor.stop)
    try:
        pet = StockPetWidget()
    except BaseException:
        stall_monitor.stop()
        raise
    stall_monitor.set_context_provider(lambda: (
        f"panel_visible={pet.panel.isVisible()} tab={pet.panel.market_tabs.currentIndex()} "
        f"watchlist_count={len(pet.panel.watchlist)} favorites_count={len(pet.favorite_symbols)} "
        f"skin={pet.current_skin} page_fetch={pet._watch_task is not None} "
        f"favorite_fetch={pet._favorite_task is not None} "
        f"detail_fetch={pet.panel._active_task is not None}"
    ))
    app.aboutToQuit.connect(pet.close_provider)
    pet.show()

    tray = QSystemTrayIcon(app_icon, app)
    tray.setToolTip(f"股票桌宠 v{__version__}")
    tray_menu = QMenu()
    show_action = QAction("显示桌宠", tray_menu)
    refresh_action = QAction("刷新当前页", tray_menu)
    hide_action = QAction("隐藏桌宠", tray_menu)
    quit_action = QAction("退出", tray_menu)

    show_action.triggered.connect(pet.show_and_raise)
    refresh_action.triggered.connect(pet.show_and_refresh)
    hide_action.triggered.connect(pet.hide_all)
    quit_action.triggered.connect(app.quit)
    tray_menu.addAction(show_action)
    tray_menu.addAction(refresh_action)
    tray_menu.addSeparator()
    tray_menu.addAction(hide_action)
    tray_menu.addAction(quit_action)
    tray.setContextMenu(tray_menu)
    tray.activated.connect(lambda reason: pet.show_and_refresh() if reason == QSystemTrayIcon.Trigger else None)
    pet.alert_requested.connect(
        lambda title, message: tray.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            8_000,
        )
    )
    tray.show()

    pet.quit_requested.connect(app.quit)
    try:
        return app.exec()
    finally:
        stall_monitor.stop()


if __name__ == "__main__":
    raise SystemExit(main())
