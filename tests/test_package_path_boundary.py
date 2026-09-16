from pathlib import Path
from unittest.mock import Mock

import boto3
import pytest
from fastapi import HTTPException

from mtm_hbl.config import Settings
from mtm_hbl.safe_paths import confined_path, filename_component
from mtm_hbl.verification.aws_repository import register_issued_package, AwsVerificationConfig
from tests.test_hbl_package_pdf import package_data


def test_registration_rejects_outside_root_before_read_or_aws(monkeypatch, tmp_path):
    root = tmp_path / 'allowed'
    root.mkdir()
    outside = tmp_path / 'private.pdf'
    outside.write_bytes(b'private')
    network = Mock(side_effect=AssertionError('AWS must not be called'))
    monkeypatch.setattr(boto3, 'client', network)
    monkeypatch.setattr(boto3, 'resource', network)
    read = Mock(side_effect=AssertionError('File must not be read'))
    monkeypatch.setattr(Path, 'read_bytes', read)
    for path in [outside, root / '..' / 'private.pdf']:
        with pytest.raises(ValueError, match='outside'):
            register_issued_package(package_data(), path, AwsVerificationConfig('b', 't', allowed_pdf_root=root))
    read.assert_not_called()
    network.assert_not_called()


def test_symlink_and_prefix_sibling_are_rejected(tmp_path):
    root = tmp_path / 'allowed'
    root.mkdir()
    outside = tmp_path / 'allowed-other'
    outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    for path in [root / 'link' / 'file.pdf', outside / 'file.pdf']:
        with pytest.raises(ValueError):
            confined_path(path, root)
    assert confined_path(root / 'nested' / 'file.pdf', root) == root / 'nested' / 'file.pdf'


@pytest.mark.parametrize('value', ['../secret.pdf', '/tmp/secret.pdf', '..', 'a\\b.pdf', 'a\x00b', '%2fetc'])
def test_filename_component_rejects_unsafe_representations(value):
    with pytest.raises(ValueError):
        filename_component(value)


@pytest.mark.parametrize('path_kind', ['outside_directory', 'absolute_filename', 'traversal_filename', 'symlink'])
def test_http_issuance_blocks_escape_before_render(monkeypatch, tmp_path, path_kind):
    from mtm_hbl.api import main
    root = tmp_path / 'runs'
    root.mkdir()
    outside = tmp_path / 'other'
    outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    output_dir, filename = str(root), 'test.pdf'
    if path_kind == 'outside_directory': output_dir = str(outside)
    if path_kind == 'absolute_filename': filename = str(outside / 'test.pdf')
    if path_kind == 'traversal_filename': filename = '../test.pdf'
    if path_kind == 'symlink': output_dir = str(root / 'link')
    render = Mock(side_effect=AssertionError('Rendering must not start'))
    monkeypatch.setattr(main, 'generate_bill_of_lading_package', render)
    request = main.PackageIssueRequest(review_packet=package_data(), output_dir=output_dir, output_filename=filename)
    with pytest.raises(HTTPException) as error:
        main.issue_dev_package(request, Settings(runs_dir=root))
    assert error.value.status_code == 422
    render.assert_not_called()


def test_http_default_preserves_per_task_output_routing(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mtm_hbl.api import main
    monkeypatch.setattr(main, 'LocalTokenStore', lambda _: SimpleNamespace(load=lambda: SimpleNamespace(access_token='offline')))
    generate = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {}))
    monkeypatch.setattr(main, 'generate_hbl_from_clickup', generate)
    asyncio.run(main.generate_hbl_from_clickup_link(main.ClickUpHblGenerationRequest(task_ref='task-1'), Settings(runs_dir=tmp_path)))
    assert generate.call_args.kwargs['output_dir'] is None
