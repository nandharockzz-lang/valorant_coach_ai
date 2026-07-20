import threading
import webbrowser

from valorant_coach.server import main


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8766")).start()
    main()
