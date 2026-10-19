"""python -m studio [--config studio.yaml] [--host 127.0.0.1] [--port 8790]"""
import argparse
import logging


def main():
    ap = argparse.ArgumentParser(prog="studio")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn
    from .api import create_app
    from .core.config import StudioConfig
    uvicorn.run(create_app(StudioConfig.load(a.config)), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
