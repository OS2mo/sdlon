from copy import deepcopy
from typing import Any
from typing import Dict
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sdlon.config import Settings

DEFAULT_MOCK_SETTINGS = {
    "integrations.SD_Lon.employment_field": "extension_1",
    "integrations.SD_Lon.global_from_date": "2022-01-09",
    "integrations.SD_Lon.import.run_db": "run_db.sqlite",
    "integrations.SD_Lon.institution_identifier": "XYZ",
    "integrations.SD_Lon.job_function": "EmploymentName",
    "integrations.SD_Lon.monthly_hourly_divide": 50000,
    "integrations.SD_Lon.sd_user": "user",
    "integrations.SD_Lon.sd_password": "password",
    "municipality.code": 740,
    "municipality.name": "Kolding Kommune",
}

DEFAULT_EXPECTED_SETTINGS: Dict[str, Any] = {
    "mora_base": "http://mo-service:5000",
    "municipality_code": 740,
    "municipality_name": "Kolding Kommune",
    "cpr_uuid_map_path": "/opt/dipex/os2mo-data-import-and-export/settings/cpr_uuid_map.csv",  # noqa
    "sd_employment_field": "extension_1",
    "sd_global_from_date": "2022-01-09",
    "sd_import_too_deep": [],
    "sd_importer_create_associations": True,
    "sd_importer_create_email_addresses": True,
    "sd_importer_employment_date_as_engagement_start_date": False,
    "sd_institution_identifier": "XYZ",
    "sd_job_function": "EmploymentName",
    "sd_monthly_hourly_divide": 50000,
    "sd_no_salary_minimum_id": None,
    "sd_password": "**********",
    "sd_skip_employment_types": [],
    "sd_use_ad_integration": True,
    "sd_phone_number_id_for_ad_creation": False,
    "sd_phone_number_id_for_ad_string": "AD-bruger fra SD",
    "sd_phone_number_id_trigger": "14",
    "sd_user": "user",
    "log_level": "DEBUG",
    "sd_persist_payloads": True,
    "job_settings": {
        "auth_realm": "mo",
        "auth_server": "http://keycloak:8080/auth",
        "client_id": "dipex",
        "client_secret": None,
        "log_format": "%(levelname)s %(asctime)s %(filename)s:%(lineno)d:%(name)s: %(message)s",  # noqa
        "log_level": "ERROR",
        "mora_base": "http://mo:5000",
    },
}

DEFAULT_FILTERED_JSON_SETTINGS = {
    "sd_employment_field": "extension_1",
    "sd_global_from_date": "2022-01-09",
    "sd_institution_identifier": "XYZ",
    "sd_job_function": "EmploymentName",
    "sd_monthly_hourly_divide": 50000,
    "sd_user": "user",
    "sd_password": "password",
    "municipality_code": 740,
    "municipality_name": "Kolding Kommune",
}

DEFAULT_CHANGED_AT_SETTINGS = {
    "municipality_name": "Kommune",
    "municipality_code": 100,
    "sd_employment_field": "extension_1",
    "sd_global_from_date": "2022-01-09",
    "sd_institution_identifier": "XY",
    "sd_job_function": "EmploymentName",
    "sd_monthly_hourly_divide": 9000,
    "sd_password": "secret",
    "sd_user": "user",
    "pghost": "sd-db",
    "app_database": "sd_payload",
    "app_dbuser": "sd_payload",
    "app_dbpassword": "secret",
}


def test_forbid_extra_settings():
    with pytest.raises(ValidationError):
        Settings(
            municipality_name="name",
            municipality_code=100,
            sd_global_from_date="1970-01-01",
            sd_employment_field="extension_1",
            sd_institution_identifier="XY",
            sd_job_function="EmploymentName",
            sd_monthly_hourly_divide=9000,
            sd_password="secret",
            sd_user="user",
            forbidden="property",
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("mora_base", "Not a URL"),
        ("municipality_code", 98),
        ("municipality_code", 1000),
        ("sd_employment_field", "extension_"),
        ("sd_employment_field", "Invalid string"),
        ("sd_global_from_date", "Invalid string"),
        ("sd_job_function", "not allowed"),
        ("sd_monthly_hourly_divide", -1),
    ],
)
def test_special_values(key, value):
    # Arrange
    mock_settings = deepcopy(DEFAULT_MOCK_SETTINGS)
    mock_settings[key] = value

    # Act and assert
    with pytest.raises(ValidationError):
        Settings.parse_obj(mock_settings)


@pytest.mark.parametrize("job_function", ["JobPositionIdentifier", "EmploymentName"])
def test_job_function_enums_allowed(job_function):
    assert Settings(
        municipality_name="name",
        municipality_code=100,
        sd_global_from_date="1970-01-01",
        sd_employment_field="extension_1",
        sd_institution_identifier="XY",
        sd_job_function=job_function,
        sd_monthly_hourly_divide=9000,
        sd_password="secret",
        sd_user="user",
        app_dbpassword="secret",
    )


@pytest.mark.parametrize(
    "key,valid_value,invalid_value",
    [
        ("sd_fix_departments_root", str(uuid4()), "not a UUID"),
        ("sd_cprs", ["1234561234", "6543214321"], ["Not CPR"]),
        ("sd_cprs", ["0000000000"], "Not list of CPRs"),
        ("sd_exclude_cprs_mode", False, "Not a boolean"),
    ],
)
def test_changed_at_settings(key, valid_value, invalid_value):
    settings = deepcopy(DEFAULT_CHANGED_AT_SETTINGS)

    # The setting is optional or has a default
    assert Settings.parse_obj(settings)

    # ... and can be set to a valid_value
    settings.update({key: valid_value})
    assert Settings.parse_obj(settings)

    # ... but not an invalid_value
    settings.update({key: invalid_value})
    with pytest.raises(ValidationError):
        Settings.parse_obj(settings)
