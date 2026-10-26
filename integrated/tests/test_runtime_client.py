import math
import pytest
from runtime_client import sample_age


@pytest.mark.parametrize('sampled', [None, True, '123', math.nan, math.inf, -math.inf])
def test_invalid_sample_timestamp_is_never_current(sampled):
    with pytest.raises(ValueError):
        sample_age({'sampled_at': sampled}, now=100)


def test_sample_age_has_explicit_boundary_and_rejects_clock_skew():
    assert sample_age({'sampled_at': 75}, now=100) == 25
    assert sample_age({'sampled_at': 100}, now=100) == 0
    for sampled in (74.9, 101):
        with pytest.raises(ValueError):
            sample_age({'sampled_at': sampled}, now=100)
