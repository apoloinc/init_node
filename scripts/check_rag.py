"""Check that the real RAG backend consumes the synthetic pilot."""
import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--enriched', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ.pop('APOLO_SETUP_MODE', None)
    os.environ['APOLO_KNOWLEDGE_DATA_ROOT'] = str(root / 'repos/apolo-knowledge-data')
    os.environ['APOLO_SKILL_STANDARD_DIR'] = str(root / 'artifacts/skill-standard')
    os.environ['APOLO_HADRON_DIR'] = str(args.enriched.resolve())
    from apolo_rag.tools.labor_market import get_labor_market
    result = get_labor_market(backend='real')
    counts = {item.skill_id: item.job_count for item in result.top_skills}
    if result.job_count != 25 or counts.get('skill_python') != 25 or counts.get('skill_sql') != 25:
        raise ValueError(f'Synthetic pilot counts do not reconcile: {result}')
    print(result.model_dump_json(indent=2))


if __name__ == '__main__':
    main()
