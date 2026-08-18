import datetime
import unittest
import uuid
from collections import OrderedDict
from datetime import date
from datetime import timedelta
from unittest import mock
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch

import hypothesis.strategies as st
import pytest
from freezegun import freeze_time
from hypothesis import given
from integrations.ad_integration.ad_reader import ADParameterReader
from parameterized import parameterized
from prometheus_client import Enum
from prometheus_client import Gauge
from ra_utils.attrdict import attrdict
from ra_utils.generate_uuid import uuid_generator

from sdlon.ad import LdapADGUIDReader
from sdlon.config import Settings
from sdlon.date_utils import format_date
from sdlon.graphql import GraphQLClient
from sdlon.it_systems import MUTATION_ADD_IT_SYSTEM_TO_EMPLOYEE
from sdlon.metrics import RunDBState
from sdlon.models import ITUserSystem
from sdlon.models import JobFunction
from sdlon.models import MOBasePerson
from sdlon.sd_changed_at import ChangeAtSD
from sdlon.sd_changed_at import changed_at

from .fixtures import get_employment_fixture
from .fixtures import get_read_employment_changed_fixture
from .fixtures import get_sd_person_fixture
from .fixtures import read_employment_fixture


class ChangeAtSDTest(ChangeAtSD):
    def __init__(self, mock_create_class: bool = True, *args, **kwargs):
        self.morahelper_mock = MagicMock()
        self.morahelper_mock.read_organisation.return_value = (
            "00000000-0000-0000-0000-000000000000"
        )
        self.primary_types_mock = MagicMock()
        self.primary_engagement_mock = MagicMock()
        self.fix_departments_mock = MagicMock()
        self.mo_graphql_client = MagicMock()

        self._get_job_sync = MagicMock()  # type: ignore

        if mock_create_class:
            self._create_class = MagicMock()  # type: ignore
            self._create_class.return_value = "new_class_uuid"

        super().__init__(*args, **kwargs)

    def _get_primary_types(self, mora_helper):
        return self.primary_types_mock

    def _get_primary_engagement_updater(self):
        return self.primary_engagement_mock

    def _get_fix_departments(self):
        return self.fix_departments_mock

    def _read_forced_uuids(self):
        return {}

    def _get_mora_helper(self, mora_base):
        return self.morahelper_mock


def setup_sd_changed_at(updates=None, hours=24, dry_run=False, mock_create_class=True):
    # TODO: remove integrations.SD_Lon.terminate_engagement_with_to_only
    settings_dict = {
        "municipality_name": "name",
        "municipality_code": 100,
        "sd_global_from_date": "1970-01-01",
        "sd_institution_identifier": "XY",
        "sd_password": "secret",
        "sd_user": "user",
        "sd_job_function": "JobPositionIdentifier",
        "sd_use_ad_integration": False,
        "sd_monthly_hourly_divide": 8000,
        "mora_base": "http://dummy.url",
        "app_dbpassword": "secret",
    }
    if updates:
        settings_dict.update(updates)

    settings = Settings.parse_obj(settings_dict)

    today = date.today()
    start_date = today

    sd_updater = ChangeAtSDTest(
        mock_create_class,
        settings,
        settings.sd_institution_identifier,
        start_date,
        start_date + timedelta(hours=hours),
        dry_run=dry_run,
    )

    return sd_updater


class Test_sd_changed_at(unittest.TestCase):
    @patch("sdlon.sd_common.requests.get")
    def test_get_sd_person(self, requests_get):
        """Test that read_person does the expected transformation."""
        cpr = "0101709999"
        sd_reply, expected_read_person_result = get_sd_person_fixture(
            cpr=cpr, first_name="John", last_name="Deere", employment_id="01337"
        )

        requests_get.return_value = sd_reply

        sd_updater = setup_sd_changed_at()
        result = sd_updater.get_sd_person(cpr=cpr)
        self.assertEqual(result, expected_read_person_result)

    @patch("sdlon.sd_changed_at.get_employee")
    def test_update_changed_persons(self, mock_get_employee: MagicMock):
        # Arrange
        cpr = "0101709999"
        first_name = "John"
        last_name = "Deere"

        _, read_person_result = get_sd_person_fixture(
            cpr=cpr,
            first_name=first_name,
            last_name=last_name,
            employment_id="01337",
        )

        sd_updater = setup_sd_changed_at()
        sd_updater.get_sd_person = lambda cpr: read_person_result

        generate_uuid = uuid_generator("test")
        org_uuid = str(generate_uuid("org_uuid"))
        user_uuid = str(generate_uuid("user_uuid"))

        sd_updater.org_uuid = org_uuid

        mock_get_employee.return_value = MOBasePerson(
            cpr=cpr,
            uuid=uuid.UUID(user_uuid),
            name=f"Old firstname {last_name}",
            givenname="Old firstname",
            surname=last_name,
        )

        morahelper = sd_updater.morahelper_mock
        _mo_post = morahelper._mo_post
        _mo_post.return_value = attrdict(
            {"status_code": 201, "json": lambda: user_uuid}
        )
        self.assertFalse(_mo_post.called)

        # Act
        sd_updater.update_changed_persons(in_cpr=cpr)

        # Assert
        _mo_post.assert_called_with(
            "e/create",
            {
                "type": "employee",
                "givenname": first_name,
                "surname": last_name,
                "cpr_no": cpr,
                "org": {"uuid": org_uuid},
                "uuid": user_uuid,
                "user_key": user_uuid,
            },
        )

    @parameterized.expand(
        [
            (True,),
            (False,),
        ]
    )
    @patch("sdlon.sd_changed_at.sd_lookup")
    def test_get_sd_persons_changed_dry_run(
        self,
        dry_run,
        mock_sd_lookup: MagicMock,
    ):
        # Arrange
        sd_updater = setup_sd_changed_at(dry_run=dry_run)

        # Act
        sd_updater.get_sd_persons_changed(datetime.datetime.now())

        # Assert
        assert mock_sd_lookup.call_args.kwargs["dry_run"] is dry_run

    @parameterized.expand(
        [
            (True,),
            (False,),
        ]
    )
    @patch("sdlon.sd_changed_at.sd_lookup")
    def test_get_sd_person_dry_run(
        self,
        dry_run,
        mock_sd_lookup: MagicMock,
    ):
        # Arrange
        sd_updater = setup_sd_changed_at(dry_run=dry_run)

        # Act
        sd_updater.get_sd_person("1212129999")

        # Assert
        assert mock_sd_lookup.call_args.kwargs["dry_run"] is dry_run

    @patch(
        "sdlon.sd_changed_at.uuid4",
        return_value=uuid.UUID("6b7f5014-faf8-11ed-aa9c-73f93fec45b0"),
    )
    @patch("sdlon.sd_changed_at.get_employee")
    @patch("sdlon.it_systems.date")
    def test_create_sd_to_ad_it_system_for_new_sd_person(
        self, mock_date: MagicMock, mock_get_employee: MagicMock, mock_uuid4: MagicMock
    ):
        """
        This test ensures that the "AD-bruger fra SD" IT-system is created on
        employees in MO for new SD persons if

        1) The environment variable SD_PHONE_NUMBER_ID_FOR_AD_CREATION is true
        2) The SD person has the appropriate string (e.g.) "14"
           in the <TelephoneNumberIdentifier> in their <ContactInformation>
        """
        # Arrange
        mock_date.today = MagicMock(return_value=date(2000, 1, 1))
        sd_updater = setup_sd_changed_at(
            updates={"sd_phone_number_id_for_ad_creation": True}
        )
        sd_updater.get_sd_persons_changed = MagicMock(
            return_value=[
                {
                    "PersonCivilRegistrationIdentifier": "1111111111",
                    "PersonGivenName": "Bruce",
                    "PersonSurnameName": "Lee",
                    "Employment": [
                        {
                            "EmploymentIdentifier": "12345",
                            "ContactInformation": {
                                "TelephoneNumberIdentifier": ["12345678", "14"]
                            },
                        },
                        {
                            "EmploymentIdentifier": "54321",
                            "ContactInformation": {
                                "TelephoneNumberIdentifier": ["87654321", "14"]
                            },
                        },
                    ],
                }
            ]
        )
        mock_execute = MagicMock(
            return_value={
                "itsystems": {
                    "objects": [{"uuid": "988dead8-7564-464a-8339-b7057bfa2665"}]
                }
            }
        )
        sd_updater.mo_graphql_client.execute = mock_execute
        mock_get_employee.return_value = None

        mock_mo_post = MagicMock(
            return_value=attrdict(
                {
                    "status_code": 201,
                    "json": lambda: "6b7f5014-faf8-11ed-aa9c-73f93fec45b0",
                }
            )
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        # Act
        sd_updater.update_changed_persons()

        # Assert
        mock_execute.assert_any_call(
            MUTATION_ADD_IT_SYSTEM_TO_EMPLOYEE,
            variable_values={
                "input": {
                    "user_key": "12345",
                    "itsystem": "988dead8-7564-464a-8339-b7057bfa2665",
                    "validity": {"from": "2000-01-01"},
                    "person": "6b7f5014-faf8-11ed-aa9c-73f93fec45b0",
                }
            },
        )
        mock_execute.assert_any_call(
            MUTATION_ADD_IT_SYSTEM_TO_EMPLOYEE,
            variable_values={
                "input": {
                    "user_key": "54321",
                    "itsystem": "988dead8-7564-464a-8339-b7057bfa2665",
                    "validity": {"from": "2000-01-01"},
                    "person": "6b7f5014-faf8-11ed-aa9c-73f93fec45b0",
                }
            },
        )

    @patch(
        "sdlon.sd_changed_at.uuid4",
        return_value=uuid.UUID("6b7f5014-faf8-11ed-aa9c-73f93fec45b0"),
    )
    @patch("sdlon.sd_changed_at.get_employee")
    @patch("sdlon.it_systems.date")
    def test_do_not_create_sd_to_ad_it_system_for_new_sd_person(
        self, mock_date: MagicMock, mock_get_employee: MagicMock, mock_uuid4: MagicMock
    ):
        """
        This test ensures that we do NOT create the "AD-bruger fra SD" IT-system
        on employees in MO for new SD persons if

        1) The environment variable SD_PHONE_NUMBER_ID_FOR_AD_CREATION is true
        2) The SD person has does NOT have the appropriate string (e.g.)
           "14" in the <TelephoneNumberIdentifier> in their
           <ContactInformation>
        """

        # Arrange
        mock_date.today = MagicMock(return_value=date(2000, 1, 1))
        sd_updater = setup_sd_changed_at(
            updates={"sd_phone_number_id_for_ad_creation": True}
        )
        sd_updater.get_sd_persons_changed = MagicMock(
            return_value=[
                {
                    "PersonCivilRegistrationIdentifier": "1111111111",
                    "PersonGivenName": "Bruce",
                    "PersonSurnameName": "Lee",
                    "ContactInformation": {"TelephoneNumberIdentifier": ["12345678"]},
                    "Employment": {"EmploymentIdentifier": "12345"},
                }
            ]
        )
        mock_execute = MagicMock(
            return_value={
                "itsystems": {
                    "objects": [{"uuid": "988dead8-7564-464a-8339-b7057bfa2665"}]
                }
            }
        )
        sd_updater.mo_graphql_client.execute = mock_execute
        mock_get_employee.return_value = None

        mock_mo_post = MagicMock(
            return_value=attrdict(
                {
                    "status_code": 201,
                    "json": lambda: "6b7f5014-faf8-11ed-aa9c-73f93fec45b0",
                }
            )
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        # Act
        sd_updater.update_changed_persons()

        # Assert
        mock_execute.assert_not_called()

    @patch(
        "sdlon.sd_changed_at.get_sd_to_ad_it_system_uuid",
        return_value=uuid.UUID("988dead8-7564-464a-8339-b7057bfa2665"),
    )
    @patch("sdlon.sd_changed_at.get_employee_it_systems", return_value=[])
    @patch("sdlon.sd_changed_at.get_employee")
    @patch("sdlon.it_systems.date")
    def test_create_sd_to_ad_it_system_for_existing_mo_person(
        self,
        mock_date: MagicMock,
        mock_get_employee: MagicMock,
        mock_get_employee_it_systems: MagicMock,
        mock_get_sd_to_ad_it_system_uuid: MagicMock,
    ):
        """
        This test ensures that the "AD-bruger fra SD" IT-system is created on
        employees in MO for SD persons already existing in MO if

        1) The environment variable SD_PHONE_NUMBER_ID_FOR_AD_CREATION is true
        2) The SD person has the appropriate string (e.g.) "14"
           in the <TelephoneNumberIdentifier> in their <ContactInformation>

           and

           The IT-system does not already exist for the employee
        """

        # Arrange
        mock_date.today = MagicMock(return_value=date(2000, 1, 1))
        sd_updater = setup_sd_changed_at(
            updates={"sd_phone_number_id_for_ad_creation": True}
        )
        sd_updater.get_sd_persons_changed = MagicMock(
            return_value=[
                {
                    "PersonCivilRegistrationIdentifier": "1111111111",
                    "PersonGivenName": "Bruce",
                    "PersonSurnameName": "Lee",
                    "Employment": {
                        "EmploymentIdentifier": "12345",
                        "ContactInformation": {
                            "TelephoneNumberIdentifier": ["12345678", "14"]
                        },
                    },
                }
            ]
        )
        mock_execute = MagicMock()
        sd_updater.mo_graphql_client.execute = mock_execute
        mock_get_employee.return_value = MOBasePerson(
            cpr="1111111111",
            givenname="Bruce",
            surname="Lee",
            name="Bruce Lee",
            uuid=uuid.UUID("6b7f5014-faf8-11ed-aa9c-73f93fec45b0"),
        )

        # Act
        sd_updater.update_changed_persons()

        # Assert
        mock_execute.assert_called_with(
            MUTATION_ADD_IT_SYSTEM_TO_EMPLOYEE,
            variable_values={
                "input": {
                    "user_key": "12345",
                    "itsystem": "988dead8-7564-464a-8339-b7057bfa2665",
                    "validity": {"from": "2000-01-01"},
                    "person": "6b7f5014-faf8-11ed-aa9c-73f93fec45b0",
                }
            },
        )

    @parameterized.expand(
        [
            (
                ["12345678", "14"],
                [
                    ITUserSystem(
                        uuid=uuid.UUID("988dead8-7564-464a-8339-b7057bfa2665"),
                        user_key="12345",
                    )
                ],
            ),
            (["12345678"], []),
        ]
    )
    @patch(
        "sdlon.sd_changed_at.get_sd_to_ad_it_system_uuid",
        return_value=uuid.UUID("988dead8-7564-464a-8339-b7057bfa2665"),
    )
    @patch("sdlon.sd_changed_at.get_employee_it_systems")
    @patch("sdlon.sd_changed_at.get_employee")
    @patch("sdlon.it_systems.date")
    def test_do_not_create_sd_to_ad_it_system_for_existing_user(
        self,
        telephone_number_ids: list[str],
        employee_it_systems: list[uuid.UUID],
        mock_date: MagicMock,
        mock_get_employee: MagicMock,
        mock_get_employee_it_systems: MagicMock,
        mock_get_sd_to_ad_it_system_uuid: MagicMock,
    ):
        """
        This test ensures that we do NOT create the "AD-bruger fra SD" IT-system
        on employees in MO for SD persons already existing in MO if

        1) The environment variable SD_PHONE_NUMBER_ID_FOR_AD_CREATION is true

        and

        2) The SD person has does NOT have the appropriate string (e.g.)
           "14" in the <TelephoneNumberIdentifier> in their
           <ContactInformation>

            or

            The IT-system is already exists for the employee.
        """

        # Arrange
        mock_date.today = MagicMock(return_value=date(2000, 1, 1))
        sd_updater = setup_sd_changed_at(
            updates={"sd_phone_number_id_for_ad_creation": True}
        )
        mock_get_employee_it_systems.return_value = employee_it_systems
        sd_updater.get_sd_persons_changed = MagicMock(
            return_value=[
                {
                    "PersonCivilRegistrationIdentifier": "1111111111",
                    "PersonGivenName": "Bruce",
                    "PersonSurnameName": "Lee",
                    "Employment": {
                        "EmploymentIdentifier": "12345",
                        "ContactInformation": {
                            "TelephoneNumberIdentifier": telephone_number_ids
                        },
                    },
                }
            ]
        )
        mock_execute = MagicMock()
        sd_updater.mo_graphql_client.execute = mock_execute
        mock_get_employee.return_value = MOBasePerson(
            cpr="1111111111",
            givenname="Bruce",
            surname="Lee",
            name="Bruce Lee",
            uuid=uuid.UUID("6b7f5014-faf8-11ed-aa9c-73f93fec45b0"),
        )

        # Act
        sd_updater.update_changed_persons()

        # Assert
        mock_execute.assert_not_called()

    @given(status=st.sampled_from(["1", "S"]))
    @patch("sdlon.sd_common.requests.get")
    def test_read_employment_changed(
        self,
        requests_get,
        status,
    ):
        sd_reply, expected_read_employment_result = read_employment_fixture(
            cpr="0101709999",
            employment_id="01337",
            job_id="1234",
            job_title="EDB-Mand",
            status=status,
        )

        requests_get.return_value = sd_reply
        sd_updater = setup_sd_changed_at()
        result = sd_updater.read_employment_changed()
        self.assertEqual(result, expected_read_employment_result)

    @patch("sdlon.sd_common.log_payload")
    @patch("sdlon.sd_common.requests.get")
    def test_read_employment_changed_dry_run(
        self,
        requests_get: MagicMock,
        mock_log_payload: MagicMock,
    ):
        # Arrange
        sd_reply, expected_read_employment_result = read_employment_fixture(
            cpr="0101709999",
            employment_id="01337",
            job_id="1234",
            job_title="EDB-Mand",
            status="1",
        )

        requests_get.return_value = sd_reply
        sd_updater = setup_sd_changed_at(dry_run=True)

        # Act
        sd_updater.read_employment_changed()

        # Assert
        mock_log_payload.assert_not_called()

    def test_do_not_create_engagement_for_inconsistent_external_emp(self):
        """
        We are testing bullet 4 in
        https://os2web.atlassian.net/browse/MO-245, i.e. that we do not
        create a MO engagement for a newly created external SD employee
        who (unintentionally) has a JobPositionIdentifier below
        no_salary_minimum.

        NOTE: an external SD employee has an EmploymentIdentifier containing
        letters (at least in some municipalities)
        """

        sd_updater = setup_sd_changed_at(
            {
                "sd_no_salary_minimum_id": 9000,
            }
        )
        sd_updater.read_employment_changed = lambda: (
            get_read_employment_changed_fixture(
                employment_id="ABCDE",
                job_pos_id=8000,  # See doc-string above
            )
        )

        morahelper = sd_updater.morahelper_mock
        morahelper.read_user.return_value.__getitem__.return_value = "user_uuid"

        sd_updater.create_new_engagement = MagicMock()

        # Act
        sd_updater.update_all_employments()

        # Assert
        sd_updater.create_new_engagement.assert_not_called()

    @given(status=st.sampled_from(["1", "S"]))
    def test_update_all_employments(self, status):
        cpr = "0101709999"
        employment_id = "01337"

        _, read_employment_result = read_employment_fixture(
            cpr=cpr,
            employment_id=employment_id,
            job_id="1234",
            job_title="EDB-Mand",
            status=status,
        )

        sd_updater = setup_sd_changed_at()
        sd_updater.read_employment_changed = lambda: read_employment_result

        morahelper = sd_updater.morahelper_mock
        morahelper.read_user.return_value.__getitem__.return_value = "user_uuid"

        if status == "1":  # Creates call create engagement, if no eng exists
            sd_updater.create_new_engagement = MagicMock()

            engagement = read_employment_result[0]["Employment"]
            # First employment status entry from read_employment_result
            status = read_employment_result[0]["Employment"]["EmploymentStatus"][0]

            self.assertFalse(sd_updater.create_new_engagement.called)
            sd_updater.update_all_employments()
            sd_updater.create_new_engagement.assert_called_with(
                engagement, status, cpr, "user_uuid"
            )
        elif status == "S":  # Deletes call terminante engagement
            morahelper.read_user_engagement.return_value = [{"user_key": employment_id}]
            sd_updater._terminate_engagement = MagicMock()

            status = read_employment_result[0]["Employment"]["EmploymentStatus"]

            self.assertFalse(sd_updater._terminate_engagement.called)
            sd_updater.update_all_employments()
            sd_updater._terminate_engagement.assert_called_with(
                user_key=employment_id,
                person_uuid="user_uuid",
                from_date=status["ActivationDate"],
            )

    @parameterized.expand(
        [
            ["07777", "monthly pay"],
            ["90000", "hourly pay"],
            ["C3-P0", "employment pay"],
        ]
    )
    def test_create_new_engagement(self, employment_id, engagement_type):
        cpr = "0101709999"
        job_id = "1234"

        _, read_employment_result = read_employment_fixture(
            cpr=cpr,
            employment_id=employment_id,
            job_id=job_id,
            job_title="EDB-Mand",
        )

        sd_updater = setup_sd_changed_at()

        sd_updater.engagement_types = {
            "månedsløn": "monthly pay",
            "timeløn": "hourly pay",
            "engagement_type" + job_id: "employment pay",
        }

        morahelper = sd_updater.morahelper_mock

        # Load noop NY logic
        sd_updater.apply_NY_logic = lambda org_unit, user_key, validity, person_uuid: (
            org_unit
        )
        # Set primary types
        sd_updater.primary_types = {
            "primary": "primary_uuid",
            "non_primary": "non_primary_uuid",
            "no_salary": "no_salary_uuid",
            "fixed_primary": "fixed_primary_uuid",
        }

        engagement = read_employment_result[0]["Employment"]
        # First employment status entry from read_employment_result
        status = read_employment_result[0]["Employment"]["EmploymentStatus"][0]

        _mo_post = morahelper._mo_post
        _mo_post.return_value = attrdict(
            {
                "status_code": 201,
            }
        )
        self.assertFalse(_mo_post.called)

        sd_updater._create_engagement_type = MagicMock()
        sd_updater._create_engagement_type.return_value = "new_engagement_type_uuid"

        sd_updater._create_professions = MagicMock()
        sd_updater._create_professions.return_value = "new_profession_uuid"

        sd_updater.create_new_engagement(engagement, status, cpr, "user_uuid")
        _mo_post.assert_called_with(
            "details/create",
            {
                "type": "engagement",
                "org_unit": {"uuid": "department_uuid"},
                "person": {"uuid": "user_uuid"},
                "job_function": {"uuid": "new_profession_uuid"},
                "engagement_type": {"uuid": engagement_type},
                "user_key": employment_id,
                "fraction": 0,
                "validity": {"from": "2020-11-10", "to": "2021-02-09"},
            },
        )
        sd_updater._create_engagement_type.assert_not_called()
        sd_updater._create_professions.assert_called_once()

    def test_terminate_engagement(self):
        employment_id = "01337"

        sd_updater = setup_sd_changed_at()
        morahelper = sd_updater.morahelper_mock

        sd_updater.mo_engagements_cache["user_uuid"] = [
            {
                "user_key": employment_id,
                "uuid": "mo_engagement_uuid",
            }
        ]

        _mo_post = morahelper._mo_post
        _mo_post.return_value = attrdict(
            {"status_code": 200, "text": lambda: "mo_engagement_uuid"}
        )
        self.assertFalse(_mo_post.called)
        sd_updater._terminate_engagement(
            user_key=employment_id, person_uuid="user_uuid", from_date="2020-11-01"
        )
        _mo_post.assert_called_once_with(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": "mo_engagement_uuid",
                "validity": {"from": "2020-11-01", "to": None},
            },
        )

    def test_terminate_engagement_returns_false_when_no_mo_engagement(self):
        sd_updater = setup_sd_changed_at()

        morahelper = sd_updater.morahelper_mock
        mock_read_user_engagement = morahelper.read_user_engagement
        mock_read_user_engagement.return_value = []

        self.assertFalse(
            sd_updater._terminate_engagement(
                user_key="12345", person_uuid=str(uuid.uuid4()), from_date="2021-10-05"
            )
        )

    def test_terminate_engagement_when_to_date_set(self):
        sd_updater = setup_sd_changed_at()
        sd_updater.mo_engagements_cache["person_uuid"] = [
            {
                "user_key": "00000",
                "uuid": "mo_engagement_uuid",
            }
        ]

        morahelper = sd_updater.morahelper_mock
        mock_post_mo = morahelper._mo_post
        mock_post_mo.return_value = attrdict(
            {"status_code": 200, "text": lambda: "mo_engagement_uuid"}
        )

        sd_updater._terminate_engagement(
            user_key="00000",
            person_uuid="person_uuid",
            from_date="2021-10-15",
            to_date="2021-10-20",
        )
        mock_post_mo.assert_called_once_with(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": "mo_engagement_uuid",
                "validity": {"from": "2021-10-15", "to": "2021-10-20"},
            },
        )

    @unittest.skip("Will be finished when fix is deployed...")
    def test_skip_sd_employments_in_skip_list(self):
        """
        Test that SD employments with JobPositionIdentifiers in the skip list
        provided in the setting "sd_skip_employment_types" are actually skipped.
        """

        # Arrange
        sd_updater = setup_sd_changed_at(
            updates={"sd_skip_employment_types": ["1", "2", "3"]}
        )

        # _, read_employment_result = read_employment_fixture(
        #     cpr="1234569999",
        #     employment_id="12345",
        #     job_id="2",  # Found in the skip list
        #     job_title="Kung Fu Fighter",
        # )
        # from pprint import pprint
        # pprint(read_employment_result)
        sd_updater.read_employment_changed = lambda: [
            OrderedDict(
                {
                    "PersonCivilRegistrationIdentifier": "1212129999",
                    "Employment": {
                        "EmploymentIdentifier": "12345",
                        "EmploymentDate": "2020-11-10",
                        "AnniversaryDate": "2004-08-15",
                        "EmploymentDepartment": {
                            "ActivationDate": "2020-11-10",
                            "DeactivationDate": "9999-12-31",
                            "DepartmentIdentifier": "department_id",
                            "DepartmentUUIDIdentifier": str(uuid.uuid4()),
                        },
                        "Profession": [
                            {
                                "ActivationDate": "2020-11-10",
                                "DeactivationDate": "2021-12-31",
                                "JobPositionIdentifier": "2",
                                "EmploymentName": "Skip this employment",
                                "AppointmentCode": "0",
                            },
                            {
                                "ActivationDate": "2022-01-01",
                                "DeactivationDate": "9999-12-31",
                                "JobPositionIdentifier": "4",
                                "EmploymentName": "Kung Fu Fighter",
                                "AppointmentCode": "0",
                            },
                        ],
                        "EmploymentStatus": {
                            "ActivationDate": "2020-11-10",
                            "DeactivationDate": "9999-12-31",
                            "EmploymentStatusCode": "1",
                        },
                    },
                }
            )
        ]

        morahelper = sd_updater.morahelper_mock
        morahelper.read_user.return_value.__getitem__.return_value = "user_uuid"

        sd_updater.create_new_engagement = MagicMock()
        sd_updater.edit_engagement = MagicMock()
        sd_updater._terminate_engagement = MagicMock()

        # Act
        sd_updater.update_all_employments()

        # Assert

        # Since the SD employment has a JobPositionIdentifier in the skip list,
        # we must ensure that engagements are not created or modified
        sd_updater.create_new_engagement.assert_not_called()
        sd_updater.edit_engagement.assert_not_called()
        sd_updater._terminate_engagement.assert_not_called()

    @parameterized.expand([("2021-10-15", "2021-10-15"), ("9999-12-31", None)])
    def test_handle_status_changes_terminates_let_go_employment_status(
        self, sd_deactivation_date, mo_termination_to_date
    ):
        # Arrange
        cpr = "0101709999"
        employment_id = "01337"

        _, read_employment_result = read_employment_fixture(
            cpr=cpr,
            employment_id=employment_id,
            job_id="1234",
            job_title="EDB-Mand",
            status="1",
        )
        sd_employment = read_employment_result[0]["Employment"]
        sd_employment["EmploymentStatus"].pop(0)  # Remove the one without status 8
        sd_employment["EmploymentStatus"][0]["DeactivationDate"] = sd_deactivation_date

        sd_updater = setup_sd_changed_at()
        sd_updater.mo_engagements_cache["person_uuid"] = [
            {
                "user_key": employment_id,
                "uuid": "mo_engagement_uuid",
            }
        ]

        morahelper = sd_updater.morahelper_mock
        mock_mo_post = morahelper._mo_post
        # The call used in _find_engagement
        mock_mo_post.return_value = attrdict(
            {"status_code": 200, "text": lambda: "mo_engagement_uuid"}
        )

        # Act
        skip = sd_updater._handle_employment_status_changes(
            cpr=cpr, sd_employment=sd_employment, person_uuid="person_uuid"
        )

        # Assert
        mock_mo_post.assert_called_once_with(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": "mo_engagement_uuid",
                "validity": {"from": "2021-02-10", "to": mo_termination_to_date},
            },
        )

        assert not skip

    def test_handle_status_change_do_not_term_non_existing_status8_sd_employment(self):
        # Arrange
        sd_updater = setup_sd_changed_at()
        sd_updater._find_last_engagement = MagicMock(return_value=None)
        sd_updater._terminate_engagement = MagicMock()

        # Act
        skip = sd_updater._handle_employment_status_changes(
            "0101011234",
            OrderedDict(
                {
                    "EmploymentIdentifier": "12345",
                    "EmploymentStatus": {
                        "ActivationDate": "2000-01-01",
                        "DeactivationDate": "2010-01-01",
                        "EmploymentStatusCode": "8",
                    },
                }
            ),
            str(uuid.uuid4()),
        )

        # Assert
        sd_updater._terminate_engagement.assert_not_called()
        assert skip

    def test_handle_status_changes_terminates_slettet_employment_status(self):
        cpr = "0101709999"
        employment_id = "01337"

        _, read_employment_result = read_employment_fixture(
            cpr=cpr,
            employment_id=employment_id,
            job_id="1234",
            job_title="EDB-Mand",
            status="S",
        )
        sd_employment = read_employment_result[0]["Employment"]

        sd_updater = setup_sd_changed_at()
        sd_updater.mo_engagements_cache["person_uuid"] = [
            {
                "user_key": employment_id,
                "uuid": "mo_engagement_uuid",
            }
        ]

        morahelper = sd_updater.morahelper_mock
        mock_mo_post = morahelper._mo_post
        mock_mo_post.return_value = attrdict(
            {"status_code": 200, "text": lambda: "mo_engagement_uuid"}
        )

        sd_updater._handle_employment_status_changes(
            cpr=cpr, sd_employment=sd_employment, person_uuid="person_uuid"
        )

        mock_mo_post.assert_called_once_with(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": "mo_engagement_uuid",
                "validity": {"from": "2020-11-01", "to": None},
            },
        )

    def test_handle_status_change_extend_eng_validity_via_status_change_only(self):
        # Arrange
        sd_updater = setup_sd_changed_at()

        sd_updater._find_last_engagement = MagicMock(
            return_value={
                "user_key": "12345",
                "uuid": "83de05b3-e890-4975-bc49-88e9052454c2",
                "validity": {
                    "from": "2000-01-01",
                    "to": "2027-01-01",  # Before the SD status 1 below ends
                },
            }
        )
        sd_updater.morahelper_mock._mo_post.return_value = attrdict(
            {"status_code": 200, "text": "response text"}
        )

        # Act
        sd_updater._handle_employment_status_changes(
            "0101011234",
            OrderedDict(
                {
                    "EmploymentIdentifier": "12345",
                    "EmploymentStatus": [
                        {
                            "ActivationDate": "2000-01-01",
                            "DeactivationDate": "2030-12-31",
                            "EmploymentStatusCode": "1",
                        },
                    ],
                }
            ),
            str(uuid.uuid4()),
        )

        # Assert
        sd_updater.morahelper_mock._mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": "83de05b3-e890-4975-bc49-88e9052454c2",
                "data": {
                    "user_key": "12345",
                    "validity": {
                        "from": "2000-01-01",
                        "to": "2030-12-31",  # The day the last active SD emp ends
                    },
                },
            },
        )

    def test_do_not_terminate_non_existing_status8_sd_employment(self):
        # Arrange
        sd_updater = setup_sd_changed_at()
        sd_updater._find_last_engagement = MagicMock(return_value=None)
        sd_updater.edit_engagement = MagicMock()

        # Act
        sd_updater._update_user_employments(
            "0101011234",
            [
                {
                    "EmploymentIdentifier": "12345",
                    "EmploymentStatus": {
                        "ActivationDate": "2000-01-01",
                        "DeactivationDate": "2010-01-01",
                        "EmploymentStatusCode": "8",
                    },
                }
            ],
            str(uuid.uuid4()),
        )

        # Assert
        sd_updater.edit_engagement.assert_not_called()

    @parameterized.expand(
        [
            ["07777", "monthly pay"],
            ["90000", "hourly pay"],
            ["C3-P0", "employment pay"],
        ]
    )
    def test_update_all_employments_editing(self, employment_id, engagement_type):
        cpr = "0101709999"
        job_id = "1234"

        _, read_employment_result = read_employment_fixture(
            cpr=cpr,
            employment_id=employment_id,
            job_id=job_id,
            job_title="EDB-Mand",
        )

        sd_updater = setup_sd_changed_at()
        sd_updater.engagement_types = {
            "månedsløn": "monthly pay",
            "timeløn": "hourly pay",
            "engagement_type" + job_id: "employment pay",
        }

        sd_updater.read_employment_changed = lambda: read_employment_result
        # Load noop NY logic
        sd_updater.apply_NY_logic = lambda org_unit, user_key, validity, person_uuid: (
            org_unit
        )

        morahelper = sd_updater.morahelper_mock
        morahelper.read_user.return_value.__getitem__.return_value = "user_uuid"
        morahelper.read_user_engagement.return_value = [
            {
                "user_key": employment_id,
                "uuid": "mo_engagement_uuid",
                "validity": {"from": "2020-11-10", "to": None},
            }
        ]

        # Set primary types
        sd_updater.primary_types = {
            "primary": "primary_uuid",
            "non_primary": "non_primary_uuid",
            "no_salary": "no_salary_uuid",
            "fixed_primary": "fixed_primary_uuid",
        }

        _mo_post = morahelper._mo_post
        _mo_post.return_value = attrdict({"status_code": 201, "text": lambda: "OK"})
        self.assertFalse(_mo_post.called)

        sd_updater._create_engagement_type = MagicMock()
        sd_updater._create_engagement_type.return_value = "new_engagement_type_uuid"

        sd_updater._create_professions = MagicMock()
        sd_updater._create_professions.return_value = "new_profession_uuid"

        sd_updater.update_all_employments()
        # We expect the exact following 4 calls to have been made
        self.assertEqual(len(_mo_post.mock_calls), 4)
        _mo_post.assert_has_calls(
            [
                call(
                    "details/edit",
                    {
                        "type": "engagement",
                        "uuid": "mo_engagement_uuid",
                        "data": {
                            "org_unit": {"uuid": "department_uuid"},
                            "validity": {"from": "2020-11-10", "to": None},
                        },
                    },
                ),
                call(
                    "details/edit",
                    {
                        "type": "engagement",
                        "uuid": "mo_engagement_uuid",
                        "data": {
                            "job_function": {"uuid": "new_profession_uuid"},
                            "validity": {"from": "2020-11-10", "to": None},
                        },
                    },
                ),
                call(
                    "details/edit",
                    {
                        "type": "engagement",
                        "uuid": "mo_engagement_uuid",
                        "data": {
                            "engagement_type": {"uuid": engagement_type},
                            "validity": {"from": "2020-11-10", "to": None},
                        },
                    },
                ),
                call(
                    "details/terminate",
                    {
                        "type": "engagement",
                        "uuid": "mo_engagement_uuid",
                        "validity": {"from": "2021-02-10", "to": None},
                    },
                ),
            ]
        )
        sd_updater._create_engagement_type.assert_not_called()
        sd_updater._create_professions.assert_called_once()

    @given(job_position=st.integers(), no_salary_minimum=st.integers())
    @patch("sdlon.sd_changed_at.sd_payloads", autospec=True)
    def test_construct_object(self, sd_payloads_mock, job_position, no_salary_minimum):
        expected = no_salary_minimum is not None
        expected = expected and job_position < no_salary_minimum
        expected = not expected

        sd_updater = setup_sd_changed_at(
            {
                "sd_no_salary_minimum_id": no_salary_minimum,
            }
        )
        sd_updater.apply_NY_logic = lambda org_unit, user_key, validity, person_uuid: (
            org_unit
        )

        morahelper = sd_updater.morahelper_mock
        morahelper.read_ou.return_value = {
            "org_unit_level": {
                "user_key": "IHaveNoIdea",
            },
            "uuid": "uuid-a",
        }
        _mo_post = morahelper._mo_post
        _mo_post.return_value = attrdict({"status_code": 201, "text": lambda: "OK"})

        engagement = {
            "EmploymentIdentifier": "BIGAL",
            "EmploymentDepartment": [{"DepartmentUUIDIdentifier": "uuid-c"}],
            "Profession": [{"JobPositionIdentifier": str(job_position)}],
        }
        status = {
            "ActivationDate": "2000-01-01",
            "DeactivationDate": "2100-01-01",
            "EmploymentStatusCode": "1",
        }
        cpr = ""
        result = sd_updater.create_new_engagement(engagement, status, cpr, "uuid-b")
        self.assertEqual(result, expected)
        if expected:
            sd_payloads_mock.create_engagement.assert_called_once()
        else:
            sd_payloads_mock.create_engagement.assert_not_called()

    @given(job_id=st.integers(min_value=0), engagement_exists=st.booleans())
    def test_fetch_engagement_type(self, job_id, engagement_exists):
        """Test that fetch_engagement_type only calls create when class is missing.

        This is done by creating the class if engagement_exists is set.
        I assume this works the same for _fetch_professions as they are similar.
        """
        sd_updater = setup_sd_changed_at()
        sd_updater.engagement_types = {
            "månedsløn": "monthly pay",
            "timeløn": "hourly pay",
        }

        self.assertEqual(len(sd_updater.engagement_types), 2)
        if engagement_exists:
            sd_updater.engagement_types["engagement_type" + str(job_id)] = (
                "old_engagement_type_uuid"
            )
            self.assertEqual(len(sd_updater.engagement_types), 3)
        else:
            self.assertEqual(len(sd_updater.engagement_types), 2)

        engagement_type_uuid = sd_updater._fetch_engagement_type(str(job_id))

        if engagement_exists:
            self.assertEqual(len(sd_updater.engagement_types), 3)
            sd_updater._create_class.assert_not_called()
            self.assertEqual(engagement_type_uuid, "old_engagement_type_uuid")
        else:
            self.assertEqual(len(sd_updater.engagement_types), 3)
            sd_updater._create_class.assert_called_once()
            sd_updater.job_sync.sync_from_sd.assert_called_once()
            self.assertIn("engagement_type" + str(job_id), sd_updater.engagement_types)
            self.assertEqual(engagement_type_uuid, "new_class_uuid")

    def test_edit_engagement(self):
        engagement = OrderedDict(
            [
                ("EmploymentIdentifier", "DEERE"),
                (
                    "Profession",
                    OrderedDict(
                        [
                            ("@changedAtDate", "1970-01-01"),
                            ("ActivationDate", "1960-01-01"),
                            ("DeactivationDate", "9999-12-31"),
                            ("JobPositionIdentifier", "9002"),
                            ("EmploymentName", "dummy"),
                            ("AppointmentCode", "0"),
                        ]
                    ),
                ),
            ]
        )

        sd_updater = setup_sd_changed_at()
        sd_updater.engagement_types = {
            "månedsløn": "monthly pay",
            "timeløn": "hourly pay",
        }
        sd_updater.mo_engagements_cache["person_uuid"] = [
            {
                "user_key": engagement["EmploymentIdentifier"],
                "uuid": "mo_engagement_uuid",
                "validity": {"from": "1950-01-01", "to": None},
            }
        ]

        morahelper = sd_updater.morahelper_mock
        _mo_post = morahelper._mo_post
        _mo_post.return_value = attrdict(
            {
                "status_code": 201,
            }
        )
        sd_updater._create_engagement_type = MagicMock(
            wraps=sd_updater._create_engagement_type
        )
        sd_updater._create_professions = MagicMock(wraps=sd_updater._create_professions)
        # Return 1 on first call, 2 on second call
        sd_updater._create_class.side_effect = [
            "new_class_1_uuid",
            "new_class_2_uuid",
        ]

        sd_updater.edit_engagement(engagement, "person_uuid", "0101011234")

        # Check that the create functions are both called
        sd_updater._create_engagement_type.assert_called_with(
            "engagement_type9002", "9002"
        )
        sd_updater._create_professions.assert_called_with("9002", "9002")
        # And thus that job_sync is called once from each
        sd_updater.job_sync.sync_from_sd.assert_has_calls(
            [call("9002", refresh=True), call("9002", refresh=True)]
        )
        # And that the results are returned to MO
        _mo_post.assert_has_calls(
            [
                call(
                    "details/edit",
                    {
                        "type": "engagement",
                        "uuid": "mo_engagement_uuid",
                        "data": {
                            "job_function": {"uuid": "new_class_1_uuid"},
                            "validity": {"from": "1960-01-01", "to": None},
                        },
                    },
                ),
                call(
                    "details/edit",
                    {
                        "type": "engagement",
                        "uuid": "mo_engagement_uuid",
                        "data": {
                            "engagement_type": {"uuid": "new_class_2_uuid"},
                            "validity": {"from": "1960-01-01", "to": None},
                        },
                    },
                ),
            ]
        )

    @patch("sdlon.sd_common.sd_lookup")
    def test_edit_engagement_job_position_id_set_to_value_above_9000(
        self,
        mock_sd_lookup,
    ):
        """
        If an employment exists in MO but with no engagement (e.g. which happens
        when the MO employment was created with an SD payload having a
        JobPositionIdentifier < 9000) and we receive an SD change payload, where
        the JobPositionIdentifier is set to a value greater than 9000, then we
        must ensure that an engagement is create for the corresponding employee
        in MO.
        """

        # Arrange

        sd_updater = setup_sd_changed_at(
            {
                "sd_monthly_hourly_divide": 80000,
                "sd_no_salary_minimum_id": 9000,
                "sd_import_too_deep": [
                    "Afdelings-niveau",
                    "NY1-niveau",
                ],
            }
        )

        sd_updater.engagement_types = {
            "månedsløn": "monthly pay",
            "timeløn": "hourly pay",
        }

        engagement = OrderedDict(
            [
                ("EmploymentIdentifier", "DEERE"),
                (
                    "Profession",
                    OrderedDict(
                        [
                            ("@changedAtDate", "2021-12-20"),
                            ("ActivationDate", "2021-12-19"),
                            ("DeactivationDate", "9999-12-31"),
                            ("JobPositionIdentifier", "9002"),
                            ("EmploymentName", "dummy"),
                            ("AppointmentCode", "0"),
                        ]
                    ),
                ),
            ]
        )

        # Necessary for the _find_engagement call in edit_engagement
        # Mock the call to arrange that no engagements are found for the user
        mora_helper = sd_updater.morahelper_mock
        _mo_lookup = mora_helper._mo_lookup
        _mo_lookup.return_value = []

        # Mock the call in sd_updater.read_employment_at(...)
        mock_sd_lookup.return_value = get_employment_fixture(
            1234561234, "emp_id", "dep_id", "dep_uuid", "9002", "job_title"
        )

        mock_apply_NY_logic = MagicMock()
        sd_updater.apply_NY_logic = mock_apply_NY_logic
        mock_apply_NY_logic.return_value = "org_unit_uuid"

        primary_types = sd_updater.primary_types_mock
        __getitem__ = primary_types.__getitem__
        __getitem__.return_value = "primary_uuid"

        _mo_post = mora_helper._mo_post
        _mo_post.return_value = attrdict(
            {
                "status_code": 201,
            }
        )

        # Act

        sd_updater.edit_engagement(engagement, "person_uuid", "0101011234")

        # Assert

        _mo_post.assert_called_once_with(
            "details/create",
            {
                "engagement_type": {"uuid": "new_class_uuid"},
                "fraction": 0,
                "job_function": {"uuid": "new_class_uuid"},
                "org_unit": {"uuid": "org_unit_uuid"},
                "person": {"uuid": "person_uuid"},
                "type": "engagement",
                "user_key": "emp_id",
                "validity": {"from": "2020-11-10", "to": "2021-02-09"},
            },
        )

    def test_edit_engagement_profession_job_position_id_set_to_value_below_9000(self):
        """
        If an employment exists in MO WITH an engagement and we receive an
        SD change payload, where the JobPositionIdentifier is set to a value
        less than 9000, then we must ensure that an engagement is terminated
        for the corresponding employee in MO.
        """

        # Arrange

        sd_updater = setup_sd_changed_at(
            {
                "sd_monthly_hourly_divide": 80000,
                "sd_no_salary_minimum_id": 9000,
                "sd_import_too_deep": [
                    "Afdelings-niveau",
                    "NY1-niveau",
                ],
            }
        )

        engagement = OrderedDict(
            [
                ("EmploymentIdentifier", "DEERE"),
                (
                    "Profession",
                    OrderedDict(
                        [
                            ("@changedAtDate", "2021-12-20"),
                            ("ActivationDate", "2021-12-19"),
                            ("DeactivationDate", "9999-12-31"),
                            ("JobPositionIdentifier", "8000"),
                            ("EmploymentName", "dummy"),
                            ("AppointmentCode", "0"),
                        ]
                    ),
                ),
            ]
        )

        mo_eng = {"user_key": "12345", "person": {"uuid": "person_uuid"}}

        # Mock the terminate engagement call
        mock_terminate_engagement = MagicMock()
        sd_updater._terminate_engagement = mock_terminate_engagement
        mock_terminate_engagement.return_value = True

        # Act

        sd_updater.edit_engagement_profession(engagement, mo_eng)

        # Assert

        mock_terminate_engagement.assert_called_once_with(
            "12345", "person_uuid", "2021-12-19", None
        )

    @patch("sdlon.sd_changed_at.update_existing_engagement")
    def test_edit_engagement_handles_empty_professions_list(self, mock_update):
        """Handle an empty `professions` list in the engagement returned by SD"""
        # This is a regression test for #47799

        # Arrange
        sd_updater = setup_sd_changed_at()
        sd_updater._find_last_engagement = lambda *args: ["mo-eng"]
        engagement = OrderedDict(
            [
                ("EmploymentIdentifier", "DEERE"),
                ("Profession", []),
            ]
        )

        # Act
        sd_updater.edit_engagement(engagement, "person_uuid", "0101011234")

        # Assert
        mock_update.assert_called_once()

    def test_do_not_edit_profession_when_sd_overwrite_emp_name_set(self):
        # Arrange
        sd_updater = setup_sd_changed_at(
            updates={
                "sd_job_function": "EmploymentName",
                "sd_overwrite_existing_employment_name": False,
            }
        )

        sd_updater._find_last_engagement = lambda *args: ["mo-eng"]
        engagement = OrderedDict(
            [
                ("EmploymentIdentifier", "12345"),
                (
                    "Profession",
                    [
                        ("@changedAtDate", "2021-12-20"),
                        ("ActivationDate", "2021-12-19"),
                        ("DeactivationDate", "9999-12-31"),
                        ("JobPositionIdentifier", "8000"),
                        ("EmploymentName", "ThisNewValueShouldNotBeWrittenToMO"),
                        ("AppointmentCode", "0"),
                    ],
                ),
            ]
        )

        sd_updater.edit_engagement_profession = MagicMock()
        sd_updater.edit_engagement_type = MagicMock()

        # Act
        sd_updater.edit_engagement(engagement, str(uuid.uuid4()), "0101011234")

        # Assert
        sd_updater.edit_engagement_profession.assert_not_called()

    @freeze_time("2000-01-01")
    @patch("sdlon.sd_changed_at.create_engagement")
    def test_edit_engagement_sd_lookup_date(self, mock_create_engagement):
        # Arrange
        sd_updater = setup_sd_changed_at(
            {
                "sd_monthly_hourly_divide": 80000,
                "sd_no_salary_minimum_id": 9000,
                "sd_import_too_deep": [
                    "Afdelings-niveau",
                    "NY1-niveau",
                ],
            }
        )

        sd_employment = OrderedDict(
            [
                ("EmploymentIdentifier", "ABCDE"),
                (
                    "Profession",
                    OrderedDict(
                        [
                            ("@changedAtDate", "2021-12-20"),
                            ("ActivationDate", "2021-12-19"),
                            ("DeactivationDate", "9999-12-31"),
                            ("JobPositionIdentifier", "9001"),
                            ("EmploymentName", "dummy"),
                            ("AppointmentCode", "0"),
                        ]
                    ),
                ),
            ]
        )

        mock__find_engagement = MagicMock(return_value=None)
        sd_updater._find_last_engagement = mock__find_engagement

        # Act
        sd_updater.edit_engagement(sd_employment, "person_uuid", "0101011234")

        # Assert
        mock_create_engagement.assert_called_once_with(
            sd_updater, "ABCDE", "person_uuid", "0101011234", date(2021, 12, 19)
        )

    @given(
        status=st.sampled_from(["1", "S"]),
        from_date=st.datetimes(),
        to_date=st.datetimes() | st.none(),
    )
    @patch("sdlon.sd_changed_at.sd_lookup")
    def test_timestamps_read_employment_changed(
        self,
        mock_sd_lookup,
        status,
        from_date,
        to_date,
    ):
        """Test that calls contain correct ActivationDate and ActivationTime"""

        sd_updater = setup_sd_changed_at()
        sd_updater.read_employment_changed(from_date=from_date, to_date=to_date)
        expected_url = "GetEmploymentChangedAtDate20111201"
        url = mock_sd_lookup.call_args.args[0]
        params = mock_sd_lookup.call_args.kwargs["params"]
        self.assertEqual(url, expected_url)
        self.assertEqual(params["ActivationDate"], from_date.strftime("%d.%m.%Y"))
        self.assertEqual(params["ActivationTime"], from_date.strftime("%H:%M"))
        if to_date:
            self.assertEqual(params["DeactivationDate"], to_date.strftime("%d.%m.%Y"))
            self.assertEqual(params["DeactivationTime"], to_date.strftime("%H:%M"))

    @given(
        status=st.sampled_from(["1", "S"]),
        from_date=st.datetimes(),
        to_date=st.datetimes() | st.none(),
    )
    @patch("sdlon.sd_changed_at.sd_lookup")
    def test_timestamps_get_sd_persons_changed(
        self,
        mock_sd_lookup,
        status,
        from_date,
        to_date,
    ):
        """Test that calls contain correct ActivationDate and ActivationTime"""
        sd_updater = setup_sd_changed_at()
        sd_updater.get_sd_persons_changed(from_date=from_date, to_date=to_date)
        expected_url = "GetPersonChangedAtDate20111201"
        url = mock_sd_lookup.call_args.args[0]
        params = mock_sd_lookup.call_args.kwargs["params"]
        self.assertEqual(url, expected_url)
        self.assertEqual(params["ActivationDate"], from_date.strftime("%d.%m.%Y"))
        self.assertEqual(params["ActivationTime"], from_date.strftime("%H:%M"))
        if to_date:
            self.assertEqual(params["DeactivationDate"], to_date.strftime("%d.%m.%Y"))
            self.assertEqual(params["DeactivationTime"], to_date.strftime("%H:%M"))


@pytest.mark.parametrize(
    "too_deep,expected_target_ou",
    [
        ([], "00000000-0000-0000-0000-000000000000"),
        (
            ["Afdelings-niveau"],
            "10000000-0000-0000-0000-000000000000",
        ),
        (
            ["Afdelings-niveau", "NY1-niveau"],
            "20000000-0000-0000-0000-000000000000",
        ),
    ],
)
def test_apply_ny_logic(too_deep: list[str], expected_target_ou: str) -> None:
    """
    Test the case where an SD employment is moved to a new SD department, which
    is an "Afdelings-niveau". The apply_NY_logic function should then return
    the UUID of the first "NY-niveau" which is not in the "too_deep" list.
    """
    # Arrange
    sd_updater = setup_sd_changed_at({"sd_import_too_deep": too_deep})

    ou_uuid_afd = "00000000-0000-0000-0000-000000000000"
    ou_uuid_ny1 = "10000000-0000-0000-0000-000000000000"
    ou_uuid_ny2 = "20000000-0000-0000-0000-000000000000"
    ou_uuid_ny3 = "30000000-0000-0000-0000-000000000000"
    person_uuid = str(uuid.uuid4())

    sd_updater.helper.read_ou = MagicMock(
        return_value={
            "uuid": ou_uuid_afd,
            "org_unit_level": {"user_key": "Afdelings-niveau"},
            "parent": {
                "uuid": ou_uuid_ny1,
                "org_unit_level": {"user_key": "NY1-niveau"},
                "parent": {
                    "uuid": ou_uuid_ny2,
                    "org_unit_level": {"user_key": "NY2-niveau"},
                    "parent": {
                        "uuid": ou_uuid_ny3,
                        "org_unit_level": {"user_key": "NY3-niveau"},
                        "parent": None,
                    },
                },
            },
        }
    )
    sd_updater.create_association = MagicMock()

    # Act
    target_ou_uuid = sd_updater.apply_NY_logic(
        ou_uuid_afd, 12345, {"from": "2023-08-01", "to": None}, person_uuid
    )

    # Assert
    assert target_ou_uuid == expected_target_ou


@pytest.mark.parametrize(
    "department_from_date,effective_fix_date",
    [
        # A date in the future
        (date(2200, 1, 1), date(2200, 1, 1)),
        # A date in the past
        (date(2000, 1, 1), datetime.datetime.now().date()),
    ],
)
def test_apply_ny_logic_for_non_existing_future_unit(
    department_from_date: date, effective_fix_date: date
) -> None:
    """
    Test the scenario where "apply_NY_logic" is called on a currently
    non-existing OU in MO, but on an SD unit which should be created
    in MO. We test that "read_ou" and "fix_department" are called with
    the correct dates.
    """
    # Arrange
    sd_updater = setup_sd_changed_at({"sd_import_too_deep": ["Afdelings-niveau"]})

    ou_uuid_afd = "00000000-0000-0000-0000-000000000000"
    ou_uuid_ny1 = "10000000-0000-0000-0000-000000000000"
    person_uuid = str(uuid.uuid4())
    department_from_date_str = format_date(department_from_date)

    mock_read_ou = MagicMock(
        side_effect=[
            {"status": 404},
            {
                "uuid": ou_uuid_afd,
                "org_unit_level": {"user_key": "Afdelings-niveau"},
                "parent": {
                    "uuid": ou_uuid_ny1,
                    "org_unit_level": {"user_key": "NY1-niveau"},
                    "parent": None,
                },
            },
        ]
    )
    sd_updater.helper.read_ou = mock_read_ou

    mock_fix_department = MagicMock()
    sd_updater.department_fixer.fix_department = mock_fix_department

    mock_create_association = MagicMock()
    sd_updater.create_association = mock_create_association

    # Act
    sd_updater.apply_NY_logic(
        ou_uuid_afd, 12345, {"from": department_from_date_str, "to": None}, person_uuid
    )

    # Assert
    assert mock_read_ou.call_args_list == [
        call(ou_uuid_afd, at=format_date(effective_fix_date), use_cache=False),
        call(ou_uuid_afd, at=format_date(effective_fix_date), use_cache=False),
    ]
    mock_fix_department.assert_called_once_with(ou_uuid_afd, effective_fix_date)
    mock_create_association.assert_called_once_with(
        ou_uuid_afd,
        person_uuid,
        12345,
        {"from": department_from_date_str, "to": None},
    )


@patch("sdlon.sd_changed_at.get_status", return_value=RunDBState.COMPLETED)
@patch("sdlon.sd_changed_at.setup_logging")
@patch("sdlon.sd_changed_at.get_settings")
@patch("sdlon.sd_changed_at.get_run_db_from_date")
@patch("sdlon.sd_changed_at.gen_date_intervals", return_value=[])
def test_dipex_last_success_timestamp_called(
    mock_get_settings: MagicMock,
    mock_setup_logging: MagicMock,
    mock_get_run_db_from_date: MagicMock,
    mock_gen_date_intervals: MagicMock,
    mock_get_run_db_state: MagicMock,
):
    # Assert
    mock_dipex_last_success_timestamp = MagicMock()
    mock_sd_changed_at_state = MagicMock()

    # Act
    changed_at(mock_dipex_last_success_timestamp, mock_sd_changed_at_state)

    # Assert
    mock_dipex_last_success_timestamp.set_to_current_time.assert_called_once()


@patch("sdlon.sd_changed_at.setup_logging")
@patch("sdlon.sd_changed_at.get_settings")
@patch("sdlon.sd_changed_at.get_run_db_from_date")
@patch("sdlon.sd_changed_at.gen_date_intervals")
def test_dipex_last_success_timestamp_not_called_on_error(
    mock_get_settings: MagicMock,
    mock_setup_logging: MagicMock,
    mock_get_run_db_from_date: MagicMock,
    mock_gen_date_intervals: MagicMock,
):
    # Assert
    mock_dipex_last_success_timestamp = MagicMock()
    mock_gen_date_intervals.side_effect = Exception()

    # Act
    with pytest.raises(Exception):
        changed_at(Gauge("Some", "gauge"), Enum("Some", "Prometheus state"))

    # Assert
    mock_dipex_last_success_timestamp.set_to_current_time.assert_not_called()


def test_only_create_leave_if_engagement_exists() -> None:
    # Arrange
    sd_employment = OrderedDict(
        {
            "EmploymentIdentifier": "12345",
            "EmploymentStatus": {
                "ActivationDate": "2020-11-10",
                "DeactivationDate": "9999-12-31",
                "EmploymentStatusCode": "3",  # Leave
            },
        }
    )

    mock_create_leave = MagicMock()

    sd_updater = setup_sd_changed_at({"sd_skip_leave_creation_if_no_engagement": True})
    sd_updater.create_leave = mock_create_leave
    sd_updater._find_last_engagement = MagicMock(
        return_value=None
    )  # No engagement found

    # Act
    sd_updater._handle_employment_status_changes(
        "1111111111", sd_employment, str(uuid.uuid4())
    )

    # Assert
    mock_create_leave.assert_not_called()


class TestEditEngagementX:
    """
    Test the re-terminate functionality of the methods:

    edit_engagement_department
    edit_engagement_profession
    edit_engagement_type
    edit_engagement_worktime
    """

    def test_edit_engagement_department_eng_not_terminated(self) -> None:
        """
        We test the case where the department of an engagement
        (which does not already have an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        org_unit_afd_level = str(uuid.uuid4())
        org_unit_ny_level = str(uuid.uuid4())
        eng_uuid = str(uuid.uuid4())
        person_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post
        sd_updater.apply_NY_logic = MagicMock(return_value=org_unit_ny_level)

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "EmploymentDepartment": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "DepartmentIdentifier": "dep1",
                "DepartmentLevelIdentifier": "NY1-niveau",
                "DepartmentName": "Department 1",
                "DepartmentUUIDIdentifier": org_unit_afd_level,
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": None,
            },
        }

        # Act
        sd_updater.edit_engagement_department(sd_payload_fragment, mo_eng, person_uuid)

        # Assert
        sd_updater.apply_NY_logic.assert_called_once_with(
            org_unit_afd_level, "12345", {"from": "1999-01-01", "to": None}, person_uuid
        )

        mock_mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "org_unit": {"uuid": org_unit_ny_level},
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

    def test_edit_engagement_department_eng_terminated(self) -> None:
        """
        We test the case where the department of an engagement
        (already having an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        org_unit = str(uuid.uuid4())
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post
        sd_updater.apply_NY_logic = MagicMock(return_value=org_unit)

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "EmploymentDepartment": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "DepartmentIdentifier": "dep1",
                "DepartmentLevelIdentifier": "NY1-niveau",
                "DepartmentName": "Department 1",
                "DepartmentUUIDIdentifier": "eb25d197-d278-41ac-abc1-cc7802093130",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": "2025-12-31",
            },
        }

        sd_updater._find_last_engagement = MagicMock(return_value=mo_eng)

        # Act
        sd_updater.edit_engagement_department(
            sd_payload_fragment, mo_eng, str(uuid.uuid4())
        )

        # Assert
        calls = mock_mo_post.call_args_list
        assert len(calls) == 2

        assert calls[0] == call(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "org_unit": {"uuid": org_unit},
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

        assert calls[1] == call(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "validity": {"from": "2026-01-01", "to": None},
            },
        )

    def test_edit_engagement_profession_eng_not_terminated(self) -> None:
        """
        We test the case where the profession of an engagement
        (which does not already have an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        job_function_uuid = str(uuid.uuid4())
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "Profession": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "EmploymentName": "Ninja",
                "JobPositionIdentifier": "1",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": None,
            },
        }

        sd_updater._fetch_professions = MagicMock(return_value=job_function_uuid)

        # Act
        sd_updater.edit_engagement_profession(sd_payload_fragment, mo_eng)

        # Assert
        sd_updater._fetch_professions.assert_called_once_with("1", "1")

        mock_mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "job_function": {"uuid": job_function_uuid},
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

    def test_edit_engagement_profession_eng_terminated(self) -> None:
        """
        We test the case where the profession of an engagement
        (which already has an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        job_function_uuid = str(uuid.uuid4())
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        sd_updater.settings.sd_job_function = JobFunction.employment_name
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "Profession": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "EmploymentName": "Ninja",
                "JobPositionIdentifier": "1",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": "2025-12-31",
            },
        }

        sd_updater._fetch_professions = MagicMock(return_value=job_function_uuid)

        # Act
        sd_updater.edit_engagement_profession(sd_payload_fragment, mo_eng)

        # Assert
        sd_updater._fetch_professions.assert_called_once_with("Ninja", "1")

        calls = mock_mo_post.call_args_list
        assert len(calls) == 2

        assert calls[0] == call(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "job_function": {"uuid": job_function_uuid},
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

        assert calls[1] == call(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "validity": {"from": "2026-01-01", "to": None},
            },
        )

    def test_edit_engagement_type_eng_terminated(self) -> None:
        """
        We test the case where the type of an engagement
        (which already has an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        eng_type_uuid = str(uuid.uuid4())
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "Profession": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "EmploymentName": "Ninja",
                "JobPositionIdentifier": "1",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": "2025-12-31",
            },
        }

        sd_updater.determine_engagement_type = MagicMock(return_value=eng_type_uuid)

        # Act
        sd_updater.edit_engagement_type(sd_payload_fragment, mo_eng)

        # Assert
        sd_updater.determine_engagement_type.assert_called_once_with(
            sd_payload_fragment, "1"
        )

        calls = mock_mo_post.call_args_list
        assert len(calls) == 2

        assert calls[0] == call(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "engagement_type": {"uuid": eng_type_uuid},
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

        assert calls[1] == call(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "validity": {"from": "2026-01-01", "to": None},
            },
        )

    def test_edit_engagement_type_eng_not_terminated(self) -> None:
        """
        We test the case where the type of an engagement
        (which does not already have an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        eng_type_uuid = str(uuid.uuid4())
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "Profession": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "EmploymentName": "Ninja",
                "JobPositionIdentifier": "1",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": None,
            },
        }

        sd_updater.determine_engagement_type = MagicMock(return_value=eng_type_uuid)

        # Act
        sd_updater.edit_engagement_type(sd_payload_fragment, mo_eng)

        # Assert
        mock_mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "engagement_type": {"uuid": eng_type_uuid},
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

    def test_edit_engagement_worktime_eng_terminated(self) -> None:
        """
        We test the case where the worktime of an engagement
        (which already has an end date) change.

        (see https://redmine.magenta.dk/issues/60402#note-16)
        """

        # Arrange
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "WorkingTime": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "OccupationRate": "0.8765",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": "2025-12-31",
            },
        }

        # Act
        sd_updater.edit_engagement_worktime(sd_payload_fragment, mo_eng)

        # Assert
        calls = mock_mo_post.call_args_list
        assert len(calls) == 2

        assert calls[0] == call(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "fraction": 876500,
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

        assert calls[1] == call(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "validity": {"from": "2026-01-01", "to": None},
            },
        )

    @pytest.mark.parametrize(
        "emp_status_deactivation_date, mo_eng_end_date, expected_end_date",
        [
            ("9999-12-31", None, None),
            ("2025-12-31", None, "2025-12-31"),
            ("2025-12-31", "2030-12-31", "2025-12-31"),
            ("2025-12-31", "2025-12-31", "2025-12-31"),
        ],
    )
    def test_edit_engagement_worktime_eng_not_terminated(
        self,
        emp_status_deactivation_date: str,
        mo_eng_end_date: str | None,
        expected_end_date: str | None,
    ) -> None:
        """
        We test the case where the worktime of an engagement
        (which does not already have an end date) change. In this test we verify that
        the engagement is not re-terminated in the case where the SD payload
        DeactivationDate is smaller than the MO engagement end date.

        See https://redmine.magenta.dk/issues/60402#note-16
        and https://redmine.magenta.dk/issues/61683
        """

        # Arrange
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "WorkingTime": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": emp_status_deactivation_date,
                "OccupationRate": "0.8765",
            },
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": mo_eng_end_date,
            },
        }

        # Act
        sd_updater.edit_engagement_worktime(sd_payload_fragment, mo_eng)

        # Assert
        mock_mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "fraction": 876500,
                    "validity": {"from": "1999-01-01", "to": expected_end_date},
                },
            },
        )

    @pytest.mark.parametrize(
        "emp_status_deactivation_date, expected_term_from_date",
        [
            ("2029-12-31", "2030-01-01"),
            ("2023-12-31", "2026-01-01"),
        ],
    )
    def test_edit_engagement_eng_terminated_when_payload_has_status_changes(
        self, emp_status_deactivation_date: str, expected_term_from_date: str
    ) -> None:
        """
        We test the case where the SD payload contains status changes and the
        worktime of an engagement (which already has an end date) change.

        See https://redmine.magenta.dk/issues/60402#note-16
        and https://redmine.magenta.dk/issues/61683
        """

        # Arrange
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "WorkingTime": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "9999-12-31",
                "OccupationRate": "0.8765",
            },
            "EmploymentStatus": [
                {
                    "ActivationDate": "2000-01-01",
                    "DeactivationDate": emp_status_deactivation_date,
                    "EmploymentStatusCode": "1",
                },
            ],
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": "2025-12-31",
            },
        }

        # Act
        sd_updater.edit_engagement_worktime(sd_payload_fragment, mo_eng)

        # Assert
        calls = mock_mo_post.call_args_list
        assert len(calls) == 2

        assert calls[0] == call(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "fraction": 876500,
                    "validity": {"from": "1999-01-01", "to": None},
                },
            },
        )

        assert calls[1] == call(
            "details/terminate",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "validity": {"from": expected_term_from_date, "to": None},
            },
        )

    def test_edit_engagement_eng_terminated_when_payload_has_status_changes_no_term(
        self,
    ) -> None:
        """
        We test the case where the SD payload contains status changes and the
        worktime of an engagement (which already has an end date) change.

        See https://redmine.magenta.dk/issues/60402#note-16
        and https://redmine.magenta.dk/issues/61683
        """

        # Arrange
        eng_uuid = str(uuid.uuid4())

        sd_updater = setup_sd_changed_at()
        mock_mo_post = MagicMock(
            return_value=attrdict({"status_code": 200, "text": "response text"}),
        )
        sd_updater.morahelper_mock._mo_post = mock_mo_post

        sd_payload_fragment = {
            "EmploymentIdentifier": "12345",
            "WorkingTime": {
                "ActivationDate": "1999-01-01",
                "DeactivationDate": "2023-12-31",
                "OccupationRate": "0.8765",
            },
            "EmploymentStatus": [
                {
                    "ActivationDate": "2000-01-01",
                    "DeactivationDate": "2025-12-31",
                    "EmploymentStatusCode": "1",
                },
            ],
        }

        mo_eng = {
            "uuid": eng_uuid,
            "validity": {
                "from": "2000-01-01",
                "to": "2025-12-31",
            },
        }

        # Act
        sd_updater.edit_engagement_worktime(sd_payload_fragment, mo_eng)

        # Assert
        calls = mock_mo_post.call_args_list
        assert len(calls) == 1

        assert calls[0] == call(
            "details/edit",
            {
                "type": "engagement",
                "uuid": eng_uuid,
                "data": {
                    "fraction": 876500,
                    "validity": {"from": "1999-01-01", "to": "2023-12-31"},
                },
            },
        )


class TestEditEngagementStatus:
    STATUS_LIST = [
        {
            "ActivationDate": "2000-01-01",
            "DeactivationDate": "2025-12-31",
            "EmploymentStatusCode": "1",
        },
        {
            "ActivationDate": "2026-01-01",
            "DeactivationDate": "2030-12-31",
            "EmploymentStatusCode": "3",
        },
        {
            "ActivationDate": "2031-01-01",
            "DeactivationDate": None,
            "EmploymentStatusCode": "8",
        },
    ]

    MO_ENG = {
        "user_key": "12345",
        "uuid": "83de05b3-e890-4975-bc49-88e9052454c2",
        "validity": {
            "from": "2000-01-01",
            "to": "2027-01-01",  # Before the SD status 3 above ends
        },
    }

    def test_edit_engagement_status(self):
        # Arrange
        sd_updater = setup_sd_changed_at()
        sd_updater.morahelper_mock._mo_post.return_value = attrdict(
            {"status_code": 200, "text": "response text"}
        )

        # Act
        sd_updater.edit_engagement_status(
            TestEditEngagementStatus.STATUS_LIST, TestEditEngagementStatus.MO_ENG
        )

        # Assert
        sd_updater.morahelper_mock._mo_post.assert_called_once_with(
            "details/edit",
            {
                "type": "engagement",
                "uuid": "83de05b3-e890-4975-bc49-88e9052454c2",
                "data": {
                    "user_key": "12345",
                    "validity": {
                        "from": "2000-01-01",
                        "to": "2030-12-31",  # The day the last active SD emp ends
                    },
                },
            },
        )

    def test_no_update_when_mo_eng_end_date_greater_than_sd_deactivation_date(self):
        # Arrange
        sd_updater = setup_sd_changed_at()
        TestEditEngagementStatus.MO_ENG["validity"]["to"] = None

        # Act
        sd_updater.edit_engagement_status(
            TestEditEngagementStatus.STATUS_LIST, TestEditEngagementStatus.MO_ENG
        )

        # Assert
        sd_updater.morahelper_mock._mo_post.assert_not_called()

    def test_dry_run(self):
        # Arrange
        sd_updater = setup_sd_changed_at()
        sd_updater.dry_run = True

        # Act
        sd_updater.edit_engagement_status(
            TestEditEngagementStatus.STATUS_LIST, TestEditEngagementStatus.MO_ENG
        )

        # Assert
        sd_updater.morahelper_mock._mo_post.assert_not_called()


@pytest.mark.parametrize(
    "prefix_enabled, sd_emp_id, sd_inst_id, expected_user_key",
    [
        (False, "12345", "II", "12345"),
        (True, "23456", "AB", "AB-23456"),
    ],
)
def test_create_new_engagement_sets_correct_user_key(
    prefix_enabled: bool,
    sd_emp_id: str,
    sd_inst_id: str,
    expected_user_key: str,
):
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_prefix_eng_user_key_with_inst_id": prefix_enabled,
            "sd_institution_identifier": sd_inst_id,
        }
    )

    person_uuid = str(uuid.uuid4())
    ou_uuid = str(uuid.uuid4())
    eng_type_uuid = str(uuid.uuid4())
    job_function_uuid = str(uuid.uuid4())

    emp_status = {
        "ActivationDate": "2000-01-01",
        "DeactivationDate": "2025-12-31",
        "EmploymentStatusCode": "1",
    }

    sd_emp = {
        "EmploymentIdentifier": sd_emp_id,
        "EmploymentStatus": emp_status,
        "EmploymentDepartment": {
            "ActivationDate": "2000-01-01",
            "DeactivationDate": "2025-12-31",
            "DepartmentIdentifier": "ABCD",
            "DepartmentUUIDIdentifier": ou_uuid,
        },
        "Profession": {
            "ActivationDate": "2000-01-01",
            "DeactivationDate": "2025-12-31",
            "JobPositionIdentifier": "4",
            "EmploymentName": "Kung Fu Fighter",
            "AppointmentCode": "0",
        },
    }

    sd_updater.apply_NY_logic = MagicMock(return_value=ou_uuid)
    sd_updater.determine_engagement_type = MagicMock(return_value=eng_type_uuid)
    sd_updater._fetch_professions = MagicMock(return_value=job_function_uuid)

    sd_updater.morahelper_mock._mo_post = MagicMock(
        return_value=attrdict({"status_code": 201, "text": "response text"})
    )

    # Act
    sd_updater.create_new_engagement(sd_emp, emp_status, "0101011234", person_uuid)

    # Assert
    sd_updater.morahelper_mock._mo_post.assert_called_once_with(
        "details/create",
        {
            "type": "engagement",
            "org_unit": {"uuid": ou_uuid},
            "person": {"uuid": person_uuid},
            "job_function": {"uuid": job_function_uuid},
            "engagement_type": {"uuid": eng_type_uuid},
            "user_key": expected_user_key,
            "fraction": 0,
            "validity": {"from": "2000-01-01", "to": "2025-12-31"},
        },
    )


def test__find_engagement_uses_correct_user_key():
    # Arrange
    sd_updater = setup_sd_changed_at()

    sd_updater._fetch_mo_engagements = MagicMock(
        return_value=[
            {"user_key": "not relevant"},
            {"user_key": "12345"},
        ]
    )

    # Act
    relevant_eng = sd_updater._find_last_engagement("12345", str(uuid.uuid4()))

    # Assert
    assert relevant_eng == {"user_key": "12345"}


@pytest.mark.parametrize(
    "prefix_enabled, sd_emp_id, sd_inst_id, expected_user_key",
    [
        (False, "12345", "II", "12345"),
        (True, "23456", "AB", "AB-23456"),
    ],
)
def test_handle_status_changes_uses_correct_user_key(
    prefix_enabled: bool,
    sd_emp_id: str,
    sd_inst_id: str,
    expected_user_key: str,
):
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_prefix_eng_user_key_with_inst_id": prefix_enabled,
            "sd_institution_identifier": sd_inst_id,
        }
    )

    sd_updater._find_last_engagement = MagicMock(
        return_value={
            "user_key": expected_user_key,
            "uuid": "83de05b3-e890-4975-bc49-88e9052454c2",
            "validity": {
                "from": "2000-01-01",
                "to": "2027-01-01",
            },
        }
    )
    sd_updater.morahelper_mock._mo_post.return_value = attrdict(
        {"status_code": 200, "text": "response text"}
    )

    # Act
    sd_updater._handle_employment_status_changes(
        "0101011234",
        OrderedDict(
            {
                "EmploymentIdentifier": "12345",
                "EmploymentStatus": [
                    {
                        "ActivationDate": "2000-01-01",
                        "DeactivationDate": "2030-12-31",
                        "EmploymentStatusCode": "1",
                    },
                ],
            }
        ),
        str(uuid.uuid4()),
    )

    # Assert
    sd_updater.morahelper_mock._mo_post.assert_called_once_with(
        "details/edit",
        {
            "type": "engagement",
            "uuid": "83de05b3-e890-4975-bc49-88e9052454c2",
            "data": {
                "user_key": expected_user_key,
                "validity": {
                    "from": "2000-01-01",
                    "to": "2030-12-31",
                },
            },
        },
    )


@pytest.mark.parametrize(
    "prefix_enabled, sd_emp_id, sd_inst_id, expected_user_key",
    [
        (False, "12345", "II", "12345"),
        (True, "23456", "AB", "AB-23456"),
    ],
)
def test_edit_engagement_uses_correct_user_key(
    prefix_enabled: bool,
    sd_emp_id: str,
    sd_inst_id: str,
    expected_user_key: str,
):
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_prefix_eng_user_key_with_inst_id": prefix_enabled,
            "sd_institution_identifier": sd_inst_id,
        }
    )

    sd_updater._find_last_engagement = MagicMock()

    person_uuid = str(uuid.uuid4())
    # We only need the EmploymentIdentifier from the payload in this test
    sd_emp = {"EmploymentIdentifier": sd_emp_id}

    # Act
    sd_updater.edit_engagement(sd_emp, person_uuid, "0101011234")

    # Assert
    sd_updater._find_last_engagement.assert_called_once_with(
        expected_user_key, person_uuid
    )


@pytest.mark.parametrize(
    "prefix_enabled, sd_emp_id, sd_inst_id, expected_user_key",
    [
        (False, "12345", "II", "12345"),
        (True, "23456", "AB", "AB-23456"),
    ],
)
def test_edit_engagement_department_uses_correct_user_key(
    prefix_enabled: bool,
    sd_emp_id: str,
    sd_inst_id: str,
    expected_user_key: str,
) -> None:
    # Arrange
    org_unit_afd_level = str(uuid.uuid4())
    org_unit_ny_level = str(uuid.uuid4())
    eng_uuid = str(uuid.uuid4())
    person_uuid = str(uuid.uuid4())

    sd_updater = setup_sd_changed_at(
        updates={
            "sd_prefix_eng_user_key_with_inst_id": prefix_enabled,
            "sd_institution_identifier": sd_inst_id,
        }
    )
    mock_mo_post = MagicMock(
        return_value=attrdict({"status_code": 200, "text": "response text"}),
    )
    sd_updater.morahelper_mock._mo_post = mock_mo_post
    sd_updater.apply_NY_logic = MagicMock(return_value=org_unit_ny_level)

    sd_payload_fragment = {
        "EmploymentIdentifier": sd_emp_id,
        "EmploymentDepartment": {
            "ActivationDate": "1999-01-01",
            "DeactivationDate": "9999-12-31",
            "DepartmentIdentifier": "dep1",
            "DepartmentLevelIdentifier": "NY1-niveau",
            "DepartmentName": "Department 1",
            "DepartmentUUIDIdentifier": org_unit_afd_level,
        },
    }

    mo_eng = {
        "uuid": eng_uuid,
        "validity": {
            "from": "2000-01-01",
            "to": None,
        },
    }

    # Act
    sd_updater.edit_engagement_department(sd_payload_fragment, mo_eng, person_uuid)

    # Assert
    sd_updater.apply_NY_logic.assert_called_once_with(
        org_unit_afd_level,
        expected_user_key,
        {"from": "1999-01-01", "to": None},
        person_uuid,
    )


def test__get_ad_reader_ad_integration_not_in_use():
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_use_ad_integration": False,
        }
    )

    # Act
    ad_reader = sd_updater._get_ad_reader()

    # Assert
    assert ad_reader is None


@patch("sdlon.sd_changed_at.ad_reader")
def test__get_ad_reader_legacy_ad_integration(mock_ad_reader: MagicMock):
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_use_ad_integration": True,
        }
    )
    mock_ad_reader.ADParameterReader.return_value = MagicMock(spec=ADParameterReader)

    # Act
    ad_reader = sd_updater._get_ad_reader()

    # Assert
    assert isinstance(ad_reader, ADParameterReader)


def test__get_ad_reader_ldap_ad_integration():
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_use_ad_integration": True,
            "sd_use_ldap_integration": True,
            "sd_ldap_host": "hostname",
            "sd_ldap_port": 1234,
        }
    )

    # Act
    ad_reader = sd_updater._get_ad_reader()

    # Assert
    assert isinstance(ad_reader, LdapADGUIDReader)
    assert ad_reader.host == "hostname"
    assert ad_reader.port == 1234


@patch("sdlon.ad.requests.get")
def test__fetch_ad_information_for_ldap_case(mock_get: MagicMock):
    # Arrange
    sd_updater = setup_sd_changed_at(
        updates={
            "sd_use_ad_integration": True,
            "sd_use_ldap_integration": True,
            "sd_ldap_host": "hostname",
            "sd_ldap_port": 1234,
        }
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "dn": "uid=bruce,ou=os2mo,o=magenta,dc=magenta,dc=dk",
        "uuid": "c04d7ec7-1364-4d98-9ad4-4dddabe703b4",
        "username": "bruce",
    }
    mock_get.return_value = mock_response

    # Act
    sam_account_name, object_guid = sd_updater._fetch_ad_information("1212121234")

    # Assert
    mock_get.assert_called_once_with(
        "http://hostname:1234/SD",
        params={"cpr_number": "1212121234"},
    )
    assert sam_account_name is None
    assert object_guid == "c04d7ec7-1364-4d98-9ad4-4dddabe703b4"


@patch("sdlon.sd_changed_at.get_mo_client")
def test__create_class(mock_get_graphql_client: MagicMock):
    # Arrange
    class_uuid = str(uuid.uuid4())
    facet_uuid = str(uuid.uuid4())

    # we can't test _create_class if we mock it
    sd_updater = setup_sd_changed_at(mock_create_class=False)

    mock_gql_client = MagicMock(spec=GraphQLClient)
    mock_get_graphql_client.return_value = mock_gql_client

    mock_execute = MagicMock(return_value={"class_create": {"uuid": class_uuid}})
    mock_gql_client.execute = mock_execute
    sd_updater.mo_graphql_client = mock_gql_client

    # Act
    result = sd_updater._create_class("user_key", "name", facet_uuid)

    # Assert
    mock_execute.assert_called_once_with(
        # HACK: graphql documents can't be compared for equality, only for identity
        # so it is pointless to assert that the query document is the same object
        # as itself and exposing the query as a global variable on sd_changed_at
        mock.ANY,
        variable_values={
            "input": {
                "name": "name",
                "user_key": "user_key",
                "facet_uuid": facet_uuid,
                "validity": {"from": "1930-01-01"},
                "scope": "TEXT",
            }
        },
    )

    assert result == class_uuid
