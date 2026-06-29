import os
import webview
from config import UI_DIR
from modules.logger import setup as setup_logger
from modules.api_bridge import Api
from modules.updater import cleanup_old_exe

UI_PATH = os.path.join(UI_DIR, "index.html")


def main():
    setup_logger()
    cleanup_old_exe()  # remove leftover .old from previous update
    api = Api()
    window = webview.create_window(
        title="vizo",
        url=UI_PATH,
        js_api=api,
        width=1440,
        height=900,
        min_size=(1024, 720),
        resizable=True,
        background_color="#0d0c12",
    )
    api.set_window(window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
