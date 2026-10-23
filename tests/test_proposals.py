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


@pytest.mark.parametrize('name', ['proposal.json', 'PR.md', 'PRD-old.md', 'prd.md'])
def test_conflicting_specs_are_rejected(tmp_path, name):
    folder = tmp_path/'one'; folder.mkdir()
    (folder/name).write_text('conflicting owner')
    with pytest.raises(ValueError, match='conflicting'):
        mod.catalogue(tmp_path)


@pytest.mark.parametrize('name', ['proposal.json', 'PR.md', '_hidden'])
def test_unknown_root_entries_cannot_hide_specs(tmp_path, name):
    item = tmp_path/name
    if name.startswith('_'): item.mkdir()
    else: item.write_text('{}')
    with pytest.raises(ValueError): mod.catalogue(tmp_path)


@pytest.mark.parametrize('directory', [True, False])
def test_linked_specs_cannot_escape_root(tmp_path, directory):
    root = tmp_path/'proposals'; root.mkdir()
    outside = tmp_path/'outside'; outside.mkdir()
    (outside/'PRD.md').write_text('outside content')
    if directory:
        (root/'one').symlink_to(outside, target_is_directory=True)
    else:
        (root/'one').mkdir()
        (root/'one'/'PRD.md').symlink_to(outside/'PRD.md')
    with pytest.raises(ValueError, match='links'):
        mod.catalogue(root)


def test_normal_attachments_and_template_are_allowed(tmp_path):
    folder=tmp_path/'one'; folder.mkdir()
    meta={'id':'one','title':'One','owner_device':'Mac-Claude','status':'active','branch':'feat/one'}
    (folder/'PRD.md').write_text('```json\n'+json.dumps(meta)+'\n```\nAC-1: result')
    (folder/'evidence').mkdir()
    (folder/'evidence'/'run.json').write_text('{}')
    (folder/'design.md').write_text('design attachment')
    (tmp_path/'_template').mkdir()
    (tmp_path/'_template'/'PRD.md').write_text('template')
    assert '[One](one/PRD.md)' in mod.catalogue(tmp_path)
