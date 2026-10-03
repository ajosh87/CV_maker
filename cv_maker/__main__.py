"""Start CV Tailor on this computer only: `python -m cv_maker` (or the `cv-tailor` command)."""
import argparse
import threading
import webbrowser

from dotenv import find_dotenv, load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(prog="cv-tailor", description="Run CV Tailor locally.")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = parser.parse_args()

    load_dotenv(find_dotenv(usecwd=True))  # same .env lookup as `flask run`

    from cv_maker.app import create_app

    app = create_app()
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    print(f"CV Tailor running at {url}  (only reachable from this computer; Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
