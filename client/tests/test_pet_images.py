"""Every picture the pets page and her photos can ask for ships in the client's image catalog."""
import json
from pathlib import Path

CATALOG = json.loads((Path(__file__).resolve().parents[1] / 'runtime/image_assets.json').read_text(encoding='utf-8'))
BREEDS = ('british-shorthair', 'orange-tabby', 'ragdoll', 'siamese', 'dragon-li', 'tuxedo')
STAGES = ('1', '2', '3', '4-slim', '4-normal', '4-chubby')
ITEMS = ('bell-collar', 'knit-sweater', 'cat-tree', 'cat-bed')


def test_all_pet_pictures_are_in_the_catalog():
    ui = CATALOG['assets']['ui']
    wanted = {f'pet-{breed}-{stage}' for breed in BREEDS for stage in STAGES} | {f'pet-item-{item}' for item in ITEMS}
    assert sorted(wanted - set(ui)) == []
    for asset_id in wanted:
        entry = ui[asset_id]
        assert entry['filename'] == asset_id + '.webp' and entry['sha256'] in entry['key']
