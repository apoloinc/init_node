"""Prepare or reconcile an isolated synthetic pilot using installed knowledge-data."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'import'])
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve()
    import pandas as pd
    from apolo_knowledge_data.ingest.vectorjobs import ingest_vectorjobs
    from apolo_knowledge_data.pipeline.export_korax import export_korax
    from apolo_knowledge_data.pipeline.import_korax import import_korax

    if args.action == 'prepare':
        run.mkdir(parents=True, exist_ok=False)
        pd.DataFrame([
            {'job_id': f'synthetic-{i:03d}', 'title': f'Analista de datos piloto {i}',
             'description_text': f'Proyecto sintetico {i}. Se requiere dominio de Python y SQL.',
             'company_name': 'Prueba sintetica'} for i in range(25)
        ]).to_parquet(run / 'feed.parquet', index=False)
        ingested = ingest_vectorjobs(run / 'feed.parquet', run / 'snapshot')
        exported = export_korax(run / 'snapshot/jobs.parquet', run / 'exchange')
        if ingested['counts']['accepted'] != 25:
            raise ValueError(f'Expected 25 accepted synthetic jobs: {ingested}')
        print(json.dumps({'ingested': ingested['counts'], 'exported': exported}))
        return

    def rows(name):
        return [json.loads(line) for line in (run / 'exchange' / name).read_text(encoding='utf-8').splitlines()]

    inputs, outputs = rows('jobs.jsonl'), rows('results.jsonl')
    expected = {row['id'] for row in inputs}
    if len(inputs) != 25 or len(expected) != 25 or len(outputs) != 25:
        raise ValueError('Expected exactly 25 unique inputs and 25 results.')
    if {row['id'] for row in outputs} != expected or not all(row['ok'] for row in outputs):
        raise ValueError('Results contain missing, duplicate, unexpected or failed jobs.')
    if (run / 'enriched').exists():
        raise FileExistsError('Choose a fresh output; enriched already exists.')
    manifest = import_korax(run / 'snapshot/jobs.parquet', run / 'exchange/mapping.parquet',
                            run / 'exchange/results.jsonl', run / 'enriched')
    if manifest['successful_result_rows'] != 25 or manifest['evidence_failed_result_rows']:
        raise ValueError(f'Reconciliation failed: {manifest}')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
