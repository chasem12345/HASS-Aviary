"""find_species: the torch-free name lookup behind the ``target`` request field."""

from app.species import Species, find_species

VOCAB = [
    Species(sci_name="Cardinalis cardinalis", com_name="Northern Cardinal"),
    Species(sci_name="Thryothorus ludovicianus", com_name="Carolina Wren"),
    Species(sci_name="Haemorhous mexicanus", com_name="House Finch"),
    Species(sci_name="Spinus tristis", com_name="American Goldfinch"),
    Species(sci_name="Spinus tristis", com_name="American Goldfinch"),   # duplicate
]


def test_exact_common_name():
    assert find_species(VOCAB, "Carolina Wren") == 1


def test_case_and_whitespace_insensitive():
    assert find_species(VOCAB, "  house FINCH ") == 2


def test_scientific_name():
    assert find_species(VOCAB, "cardinalis cardinalis") == 0


def test_unknown_blank_and_none():
    assert find_species(VOCAB, "Dodo") is None
    assert find_species(VOCAB, "   ") is None
    assert find_species(VOCAB, None) is None


def test_excluded_index_is_not_returned():
    assert find_species(VOCAB, "Northern Cardinal", excluded=[0]) is None
    assert find_species(VOCAB, "Northern Cardinal", excluded=[1]) == 0


def test_first_match_wins_on_duplicates():
    assert find_species(VOCAB, "American Goldfinch") == 3
