"""
CLI 진입점.
  python -m formulator --data data.csv --product product.csv --query "..."
  python formulator/main.py --data ...
"""

import argparse
import os

from formulator.config import DEFAULT_AWS_REGION, DEFAULT_MODEL_ID
from formulator.pipeline import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="화장품 처방 자동 생성 PoC v1.0 (AWS Bedrock)"
    )
    parser.add_argument("--data",        required=True,  help="처방 CSV (data.csv)")
    parser.add_argument("--product",     required=True,  help="마케팅 키워드 CSV (product.csv)")
    parser.add_argument("--external",    default="external.csv",
                        help="타사 제품 CSV (v1.0에서 미사용, 호환성 유지)")
    parser.add_argument("--query",       required=True,  help="제품 요구사항 텍스트")
    parser.add_argument("--aws_profile", default=None,   help="AWS 프로파일명 (로컬 개발용)")
    parser.add_argument(
        "--aws_region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION),
        help="AWS 리전 (기본: ap-northeast-2)",
    )
    parser.add_argument("--model",  default=DEFAULT_MODEL_ID, help="Bedrock 모델 ID")
    parser.add_argument("--output", default="output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_pipeline(
        data_csv    = args.data,
        product_csv = args.product,
        external_csv= args.external,
        query       = args.query,
        aws_profile = args.aws_profile,
        aws_region  = args.aws_region,
        model_id    = args.model,
        output_dir  = args.output,
    )


if __name__ == "__main__":
    main()
