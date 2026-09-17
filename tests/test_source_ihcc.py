"""Tests for the IHCC cohort normalizer."""
import source_ihcc as si


def test_humanize_acronyms():
    assert si._humanize("genomic_data_wgs") == "Genomic Data WGS"
    assert si._humanize("electronic_health_record_data").endswith("Data")


def test_present_filters_absent_bands():
    present = si._present({"biospecimens": "51-75%", "imaging_data": "No",
                           "x": "0%", "y": True, "z": False})
    assert "Biospecimens" in present
    assert "Imaging Data" not in present  # "No" is absent
    assert "X" not in present             # "0%" is absent
    assert "Y" in present                 # boolean True is present


def test_as_list_handles_str_and_list():
    assert si._as_list(["a", "b"]) == ["a", "b"]
    assert si._as_list("Canada") == ["Canada"]
    assert si._as_list(None) == []


def test_norm_ihcc_first_record(ihcc_raw):
    docs = si.norm_ihcc(ihcc_raw)
    d = docs[0]
    assert d["id"] == "ihcc-canadian-partnership"
    assert d["source"] == "ihcc" and d["record_type"] == "Cohort"
    assert d["countries"] == ["Canada"]
    assert d["enrollment"] == 345000
    assert "Biospecimens" in d["data_types"]
    assert "Imaging Data" not in d["data_types"]
    assert d["url"] == "https://canpath.ca/"
    assert "345,000 participants" in d["snippet"]


def test_norm_ihcc_dup_name_and_fallbacks(ihcc_raw):
    docs = si.norm_ihcc(ihcc_raw)
    # blank cohort_name record skipped -> 2 records
    assert len(docs) == 2
    d2 = docs[1]
    assert d2["id"] != docs[0]["id"], "duplicate names must get distinct ids"
    assert d2["countries"] == ["Canada"]          # string coerced to list
    assert d2["url"] == si.ATLAS_HOME             # empty website -> atlas home
    assert d2["enrollment"] == ""                 # 0 enrollment -> blank


def test_ihcc_source_registered():
    assert [s.key for s in si.SOURCES] == ["ihcc"]
