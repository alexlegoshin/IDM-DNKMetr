import argparse

import yaml

from measure import run_session


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="ДНК-метр — стенд калибровки датчиков")
    parser.add_argument("--config", default="config.yaml", help="Путь к config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_session(cfg)


if __name__ == "__main__":
    main()
