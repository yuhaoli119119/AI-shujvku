#!/usr/bin/env python3
"""Read-only validation of the U137-U144 source package; never writes to a database."""
import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

PAPERS = {
    'U137': 'cd8044d4-e4f2-4255-9b38-0825f17f3559',
    'U138': '770ee075-6304-499c-8cce-70aff00181cb',
    'U139': '71fc5d35-e66b-44ae-9724-61008df8cf84',
    'U140': 'a1ea7faf-a9b1-450e-b014-558eec650483',
    'U141': '6658553c-33ac-46aa-83b7-740ea484b5c3',
    'U142': '45fb724f-071d-43f5-93c8-36b5bf897fec',
    'U143': '1cb60542-7aca-4252-94fc-9ffeacd1b9a9',
    'U144': 'b79eeb77-ad51-4459-bc34-e33c0b8b04ae',
}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def inspect(root):
    import openpyxl
    root = root.resolve(strict=True)
    manifest = json.loads((root / 'DATA_SOURCES_MANIFEST.json').read_text(encoding='utf-8'))
    if digest(root / 'README.md') != manifest['readme_sha256']:
        raise ValueError('README hash mismatch')
    if {p['paper'] for p in manifest['papers']} != set(PAPERS):
        raise ValueError('unexpected paper scope')
    files = 0
    for paper in manifest['papers']:
        for name, info in paper.get('file_hashes', {}).items():
            if Path(name).name != name:
                raise ValueError('invalid filename')
            f = (root / paper['paper'] / name).resolve(strict=True)
            if not f.is_relative_to(root / paper['paper']) or not f.is_file():
                raise ValueError('invalid file path')
            if f.stat().st_size != info['bytes'] or digest(f) != info['sha256']:
                raise ValueError(f'hash or size mismatch: {paper["paper"]}/{name}')
            files += 1
    if files != 22:
        raise ValueError(f'unexpected file count: {files}')
    with ZipFile(root / 'U137/u137-github-v1.1.0.zip') as z:
        source_counts = {}
        for n in ('data_2A.csv', 'data_3A.csv'):
            members = [x for x in z.namelist() if x.endswith('/data/' + n)]
            if len(members) != 1:
                raise ValueError(f'bad ZIP member: {n}')
            with z.open(members[0]) as f:
                source_counts[n] = len(list(csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig', newline=''))))
    if source_counts != {'data_2A.csv': 23, 'data_3A.csv': 28}:
        raise ValueError('U137 row count mismatch')
    rows = csv_rows(root / 'U141/u141_ML4LiS_descriptor_dataset_95rows.csv')
    species = Counter(row['adsorbate'] for row in rows)
    expected = {x: 19 for x in ('Li2S', 'Li2S2', 'Li2S4', 'Li2S6', 'Li2S8')}
    if len(rows) != 95 or species != expected:
        raise ValueError('U141 counts mismatch')
    wb = openpyxl.load_workbook(root / 'U142/u142_SI_Tables_S1-S7.xlsx', read_only=True, data_only=True)
    counts = {}
    for sheet in wb:
        if sheet.title == 'Metadata':
            continue
        key = sheet.title.split('_', 1)[0]
        if key in counts:
            raise ValueError('duplicate U142 table')
        counts[key] = sum(1 for row in list(sheet.values)[1:] if any(v is not None for v in row))
    wb.close()
    if counts != dict(zip(('S1','S2','S3','S4','S5','S6','S7'), (15,16,24,16,20,7,15))):
        raise ValueError('U142 table counts mismatch')
    profile = {}
    for name, filename, count in (
        ('all', 'u138_all_structures_energy_labels_9927rows.csv', 9927),
        ('training', 'u138_paper_training_7793rows.csv', 7793),
        ('validation', 'u138_paper_validation_last50_195rows.csv', 195),
    ):
        profile[name] = len(csv_rows(root / 'U138' / filename))
        if profile[name] != count:
            raise ValueError(f'U138 {name} row count mismatch')
    return {'mode': 'read-only preview', 'files_verified': files, 'paper_ids_expected_not_live_verified': PAPERS,
            'u137_source_rows': source_counts, 'u138_profile': profile,
            'u141_rows': len(rows), 'u141_adsorbates': dict(species), 'u142_rows_by_table': counts,
            'not_verified': ['production paper mapping', 'DOCX cell provenance', 'semantic overlap with existing rows']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.source_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
