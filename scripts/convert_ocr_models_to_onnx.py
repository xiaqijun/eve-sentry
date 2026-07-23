"""Convert cached PP-OCRv6 medium Paddle models to RapidOCR ONNX files."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import onnx
import yaml

MODEL_NAMES = ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / ".paddlex" / "official_models",
        help="PaddleX model cache containing both PP-OCRv6 medium models",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/onnx-models"),
        help="output root for converted model.onnx files",
    )
    parser.add_argument("--opset", type=int, default=17)
    return parser.parse_args()


def convert_model(
    paddle2onnx: str,
    source_dir: Path,
    output_path: Path,
    opset: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            paddle2onnx,
            "--model_dir",
            str(source_dir),
            "--model_filename",
            "inference.json",
            "--params_filename",
            "inference.pdiparams",
            "--save_file",
            str(output_path),
            "--opset_version",
            str(opset),
            "--enable_onnx_checker",
            "True",
        ],
        check=True,
    )


def add_recognition_metadata(source_dir: Path, model_path: Path) -> int:
    config = yaml.safe_load((source_dir / "inference.yml").read_text(encoding="utf-8"))
    characters = config["PostProcess"]["character_dict"]
    model = onnx.load(str(model_path))
    del model.metadata_props[:]
    metadata = model.metadata_props.add()
    metadata.key = "character"
    metadata.value = "\n".join(characters)
    onnx.checker.check_model(model)
    onnx.save(model, str(model_path))
    return len(characters)


def main() -> int:
    args = parse_args()
    paddle2onnx = shutil.which("paddle2onnx")
    if not paddle2onnx:
        raise RuntimeError("paddle2onnx executable is not available on PATH")

    for model_name in MODEL_NAMES:
        source_dir = args.source / model_name
        required = (source_dir / "inference.json", source_dir / "inference.pdiparams")
        if not all(path.is_file() for path in required):
            raise FileNotFoundError(f"Incomplete Paddle model: {source_dir}")
        output_path = args.output / model_name / "model.onnx"
        convert_model(paddle2onnx, source_dir, output_path, args.opset)
        print(f"Converted {model_name}: {output_path.stat().st_size / 1024 / 1024:.2f} MB")

    rec_source = args.source / "PP-OCRv6_medium_rec"
    rec_model = args.output / "PP-OCRv6_medium_rec" / "model.onnx"
    character_count = add_recognition_metadata(rec_source, rec_model)
    print(f"Embedded {character_count} recognition characters into {rec_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
