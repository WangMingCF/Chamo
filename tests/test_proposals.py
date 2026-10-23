"""The catalogue cannot silently drop ownership or diverge from its PRDs."""
import importlib.util
import json
from pathlib import Path
import pytest
spec = importlib.util.spec_from_file_location('catalogue', Path(__file__).resolve().parents[1]/'scripts/check_proposals.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_catalogue_links_the_single_spec(tmp_path):
    folder=tmp_path/'one'; folder.mkdir()
    data={'id':'one','title':'One','owner_device':'Mac-Codex','status':'active','branch':'feat/one'}
    (folder/'PRD.md').write_text('```json\n'+json.dumps(data)+'\n```\nAC-1: observable result')
    assert '[One](one/PRD.md)' in mod.catalogue(tmp_path)
    data['owner_device']='unknown'
    (folder/'PRD.md').write_text('```json\n'+json.dumps(data)+'\n```\nAC-1: result')
    with pytest.raises(ValueError, match='device'): mod.catalogue(tmp_path)
