"""Optional suite: renders the package in a real test Home Assistant.

    pip install pytest-homeassistant-custom-component pyyaml
    cd tests/homeassistant && pytest
"""
import pytest

pytest.importorskip('pytest_homeassistant_custom_component')
