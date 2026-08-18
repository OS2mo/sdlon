import datetime
import sys
import uuid
from functools import lru_cache
from functools import partial
from itertools import tee
from operator import itemgetter
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import OrderedDict
from typing import Set
from typing import Tuple
from typing import Union
from typing import cast
from uuid import UUID
from uuid import uuid4
from zoneinfo import ZoneInfo

import click
import requests
from fastapi.encoders import jsonable_encoder
from gql import gql
from integrations.ad_integration import ad_reader
from more_itertools import last
from more_itertools import one
from more_itertools import partition
from os2mo_helpers.mora_helpers import MoraHelper
from prometheus_client import Enum
from prometheus_client import Gauge
from ramodels.mo import Employee
from ramodels.mo._shared import OrganisationRef
from structlog.stdlib import get_logger

from db.queries import get_run_db_from_date
from db.queries import get_status
from db.queries import persist_status
from sdlon.ad import LdapADGUIDReader
from sdlon.employees import get_employee
from sdlon.exceptions import PreviousRunNotCompletedError
from sdlon.graphql import get_mo_client
from sdlon.it_systems import add_it_system_to_employee
from sdlon.it_systems import get_employee_it_systems
from sdlon.it_systems import get_sd_to_ad_it_system_uuid
from sdlon.log import anonymize_cpr
from sdlon.log import setup_logging
from sdlon.metrics import RunDBState
from sdlon.metrics import dipex_last_success_timestamp
from sdlon.metrics import sd_changed_at_state
from sdlon.sd_to_pydantic import convert_to_sd_base_person

from . import sd_payloads
from .config import Settings
from .config import get_settings
from .date_utils import create_eng_lookup_date
from .date_utils import date_to_datetime
from .date_utils import format_date
from .date_utils import gen_date_intervals
from .date_utils import parse_datetime
from .date_utils import sd_to_mo_date
from .date_utils import sd_to_mo_validity
from .engagement import create_engagement
from .engagement import engagement_components
from .engagement import filtered_professions
from .engagement import get_eng_user_key
from .engagement import is_employment_id_and_no_salary_minimum_consistent
from .engagement import re_terminate_engagement
from .engagement import terminate_eng_from_uuid
from .engagement import update_existing_engagement
from .fix_departments import FixDepartments
from .models import JobFunction
from .models import MOBasePerson
from .models import SDBasePerson
from .sd_common import EmploymentStatus
from .sd_common import calc_employment_id
from .sd_common import ensure_list
from .sd_common import mora_assert
from .sd_common import sd_lookup
from .skip import cpr_env_filter
from .skip import is_valid_cpr
from .sync_job_id import JobIdSync

DUMMY_CPR = "0000000000"

logger = get_logger()


# TODO: SHOULD WE IMPLEMENT PREDICTABLE ENGAGEMENT UUIDS ALSO IN THIS CODE?!?


class ChangeAtSD:
    def __init__(
        self,
        settings: Settings,
        current_inst_id: str,
        from_date: datetime.datetime,
        to_date: Optional[datetime.datetime] = None,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.dry_run = dry_run
        self.current_inst_id = current_inst_id

        # No more service API... let's get started using GraphQL!
        self.mo_graphql_client = get_mo_client(
            settings.job_settings.auth_server,
            settings.job_settings.client_id,
            settings.job_settings.client_secret,
            settings.mora_base,
            22,
        )

        self.department_fixer = self._get_fix_departments()
        self.helper = self._get_mora_helper(self.settings.mora_base)
        self.job_sync = self._get_job_sync(self.settings)

        self.use_ad = self.settings.sd_use_ad_integration

        # See https://os2web.atlassian.net/browse/MO-245 for more details
        # about no_salary_minimum
        self.no_salary_minimum = self.settings.sd_no_salary_minimum_id

        try:
            self.org_uuid = self.helper.read_organisation()
        except requests.exceptions.RequestException as e:
            logger.exception("Could not read MO organization", error=e)
            exit()

        self.from_date = from_date
        self.to_date = to_date

        # Cache of mo engagements
        self.mo_engagements_cache: Dict[str, list] = {}

        logger.info("Read job_functions")
        facet_info = self.helper.read_classes_in_facet("engagement_job_function")
        job_functions = facet_info[0]
        self.job_function_facet = facet_info[1]
        # Map from user-key to uuid if jpi, name to uuid otherwise
        job_function_mapper = cast(
            Callable[[Any], Tuple[str, str]], itemgetter("name", "uuid")
        )
        if self.settings.sd_job_function is JobFunction.job_position_identifier:
            job_function_mapper = cast(
                Callable[[Any], Tuple[str, str]], itemgetter("user_key", "uuid")
            )
        self.job_functions: Dict[str, str] = dict(
            map(job_function_mapper, job_functions)
        )

        logger.info("Read engagement types")
        # The Opus diff-import contains a slightly more abstrac def to do this
        engagement_types = self.helper.read_classes_in_facet("engagement_type")
        self.engagement_type_facet = engagement_types[1]
        engagement_type_mapper = cast(
            Callable[[Any], Tuple[str, str]], itemgetter("user_key", "uuid")
        )
        self.engagement_types: Dict[str, str] = dict(
            map(engagement_type_mapper, engagement_types[0])
        )

        # SD supports only one type of leave
        self.leave_uuid = self.helper.ensure_class_in_facet("leave_type", "Orlov")
        # SD supports only one type of association
        self.association_uuid = self.helper.ensure_class_in_facet(
            "association_type", "SD-Medarbejder"
        )

    def _get_fix_departments(self) -> FixDepartments:
        return FixDepartments(self.settings, self.current_inst_id, self.dry_run)

    def _get_mora_helper(self, mora_base) -> MoraHelper:
        return MoraHelper(hostname=mora_base, use_cache=False)

    def _get_job_sync(self, settings: Settings) -> JobIdSync:
        return JobIdSync(settings, self.current_inst_id, self.mo_graphql_client)

    @lru_cache(maxsize=None)
    def _get_ad_reader(self):
        if self.use_ad:
            if self.settings.sd_use_ldap_integration:
                return LdapADGUIDReader(
                    self.settings.sd_ldap_host, self.settings.sd_ldap_port
                )
            logger.info("AD integration in use")
            return ad_reader.ADParameterReader()
        logger.info("AD integration not in use")
        return None

    def _fetch_ad_information(self, cpr) -> Union[Tuple[None, None], Tuple[str, str]]:
        ad_reader = self._get_ad_reader()
        if ad_reader is None:
            return None, None

        ad_info = ad_reader.read_user(cpr=cpr)
        object_guid = ad_info.get("ObjectGuid", None)
        sam_account_name = ad_info.get("SamAccountName", None)
        return sam_account_name, object_guid

    @lru_cache(maxsize=None)
    def _fetch_ad_it_system_uuid(self):
        if not self.use_ad:
            raise ValueError("_fetch_ad_it_system_uuid called without AD enabled")
        it_systems = self.helper.read_it_systems()
        return one(
            map(
                itemgetter("uuid"),
                filter(lambda system: system["name"] == "Active Directory", it_systems),
            )
        )

    def _create_sd_to_ad_it_system_connection(
        self, employee_uuid: UUID, user_key: str
    ) -> None:
        """
        Create an "AD-bruger fra SD" IT-system connection.

        Args:
            employee_uuid: UUID of the MO employee
            user_key: user_key (must be the SD EmploymentIdentifier) of
              the IT-user
        """
        sd_to_ad_it_system_uuid = get_sd_to_ad_it_system_uuid(
            self.mo_graphql_client, self.settings.sd_phone_number_id_for_ad_string
        )

        if self.dry_run:
            logger.debug(
                "Dry-run: add IT-system to employee",
                emp_uuid=employee_uuid,
                it_system_uuid=sd_to_ad_it_system_uuid,
                user_key=user_key,
            )
            return

        add_it_system_to_employee(
            self.mo_graphql_client,
            employee_uuid,
            sd_to_ad_it_system_uuid,
            user_key,
        )

    @lru_cache(maxsize=None)
    def read_employment_changed(
        self,
        from_date: Optional[datetime.datetime] = None,
        to_date: Optional[datetime.datetime] = None,
        employment_identifier: Optional[str] = None,
        in_cpr: Optional[str] = None,
    ):
        from_date = from_date or self.from_date
        to_date = to_date or self.to_date

        params = {
            "ActivationDate": from_date.strftime("%d.%m.%Y"),
            "ActivationTime": from_date.strftime("%H:%M"),
            "DepartmentIndicator": "true",
            "EmploymentStatusIndicator": "true",
            "ProfessionIndicator": "true",
            "WorkingTimeIndicator": "true",
            "UUIDIndicator": "true",
            "StatusPassiveIndicator": "true",
            "SalaryAgreementIndicator": "false",
            "SalaryCodeGroupIndicator": "false",
        }
        if employment_identifier:
            params.update(
                {
                    "EmploymentIdentifier": employment_identifier,
                }
            )
        if in_cpr:
            params.update(
                {
                    "PersonCivilRegistrationIdentifier": in_cpr,
                }
            )

        if to_date is not None:
            url = "GetEmploymentChangedAtDate20111201"
            params.update(
                {
                    "DeactivationDate": to_date.strftime("%d.%m.%Y"),
                    "DeactivationTime": to_date.strftime("%H:%M"),
                    "StatusActiveIndicator": "true",
                    "StatusPassiveIndicator": "true",
                    "FutureInformationIndicator": "true",
                }
            )
        else:
            url = "GetEmploymentChanged20111201"
            params.update(
                {
                    "DeactivationDate": "31.12.9999",
                }
            )

        request_uuid = uuid.uuid4()
        logger.info("read_employment_changed", request_uuid=request_uuid)
        response = sd_lookup(
            url,
            settings=self.settings,
            params=params,
            request_uuid=request_uuid,
            dry_run=self.dry_run,
            institution_identifier=self.current_inst_id,
        )

        employment_response = ensure_list(response.get("Person", []))

        return employment_response

    def get_sd_persons_changed(
        self,
        from_date: datetime.datetime,
        to_date: datetime.datetime | None = None,
        cpr: str | None = None,
    ) -> List[OrderedDict[str, Any]]:
        """
        Get list of SD Løn persons that have changed between `from_date`
        and `to_date`

        Returns:
            List of SD Løn persons changed between the two dates
        """

        params = {
            "ActivationDate": from_date.strftime("%d.%m.%Y"),
            "ActivationTime": from_date.strftime("%H:%M"),
            "DeactivationDate": "31.12.9999",
            "StatusActiveIndicator": "true",
            "StatusPassiveIndicator": "true",
            "ContactInformationIndicator": str(
                self.settings.sd_phone_number_id_for_ad_creation
            ).lower(),
            "PostalAddressIndicator": "false",
            # TODO: Er der kunder, som vil udlæse adresse-information?
        }
        if cpr is not None:
            params["PersonCivilRegistrationIdentifier"] = cpr
        if to_date:
            params["DeactivationDate"] = to_date.strftime("%d.%m.%Y")
            params["DeactivationTime"] = to_date.strftime("%H:%M")

        request_uuid = uuid.uuid4()
        logger.info("get_sd_persons_changed", request_uuid=request_uuid)
        url = "GetPersonChangedAtDate20111201"
        response = sd_lookup(
            url,
            settings=self.settings,
            params=params,
            request_uuid=request_uuid,
            dry_run=self.dry_run,
            institution_identifier=self.current_inst_id,
        )
        persons_changed = ensure_list(response.get("Person", []))
        return persons_changed

    def get_sd_person(self, cpr: str) -> List[OrderedDict[str, Any]]:
        """
        Get a single person from SD Løn at `self.from_date`

        Args:
            cpr: the cpr number of the person

        Returns:
            A list containing the person (or an empty list if no person
            is found)
        """

        params = {
            "EffectiveDate": self.from_date.strftime("%d.%m.%Y"),
            "PersonCivilRegistrationIdentifier": cpr,
            "StatusActiveIndicator": "True",
            "StatusPassiveIndicator": "false",
            "ContactInformationIndicator": str(
                self.settings.sd_phone_number_id_for_ad_creation
            ).lower(),
            "PostalAddressIndicator": "false",
        }
        url = "GetPerson20111201"
        request_uuid = uuid.uuid4()
        logger.info("get_sd_person", request_uuid=request_uuid)
        response = sd_lookup(
            url,
            settings=self.settings,
            params=params,
            request_uuid=request_uuid,
            dry_run=self.dry_run,
            institution_identifier=self.current_inst_id,
        )
        person = ensure_list(response.get("Person", []))
        return person

    def update_changed_persons(
        self,
        in_cpr: str | None = None,
        changed_at_run_cpr: str | None = None,
    ) -> None:
        """Update and insert (upsert) changed persons.

        Args:
            in_cpr: Optional CPR number of a specific person to upsert instead of
                    using SDs GetPersonChangedAtDate endpoint.
            changed_at_run_cpr: If set, SD-changed-at will only run the
                function for the given CPR

        Note:
            This method does not create employments at all, as this responsibility is
            handled by the update_employment method instead.
        """

        assert not (in_cpr is not None and changed_at_run_cpr is not None), (
            "in_cpr and changed_at_run_cpr cannot be set simultaneously"
        )

        def fetch_mo_person(person: SDBasePerson) -> MOBasePerson | None:
            employee = get_employee(self.mo_graphql_client, person.cpr)
            return employee

        def upsert_employee(
            uuid: str, given_name: Optional[str], sur_name: Optional[str], cpr: str
        ) -> str:
            model = Employee(
                uuid=uuid,
                user_key=uuid,
                givenname=given_name,
                surname=sur_name,
                cpr_no=cpr,
                org=OrganisationRef(uuid=self.org_uuid),
            )
            payload = jsonable_encoder(model.dict(by_alias=True, exclude_none=True))
            if self.dry_run:
                logger.debug(
                    "Dry-run: upsert_employee",
                    payload=model.dict(by_alias=True, exclude={"cpr_no"}),
                )
                return "invalid-uuid"
            response = self.helper._mo_post("e/create", payload)
            assert response.status_code == 201
            return_uuid = response.json()
            logger.info(
                "Created or updated employee",
                given_name=given_name,
                sur_name=sur_name,
                return_uuid=return_uuid,
            )
            return return_uuid

        def create_itsystem_connection(sam_account_name: str, user_uuid: str):
            payload = sd_payloads.connect_it_system_to_user(
                sam_account_name, self._fetch_ad_it_system_uuid(), user_uuid
            )
            logger.debug("Create IT-system connection", payload=payload)
            if self.dry_run:
                return
            response = self.helper._mo_post("details/create", payload)
            assert response.status_code == 201
            logger.info("Added AD account info to user", user_uuid=user_uuid)

        # Fetch a list of persons to update
        if in_cpr is not None:
            all_sd_persons_changed = self.get_sd_person(in_cpr)
        else:
            all_sd_persons_changed = self.get_sd_persons_changed(
                self.from_date, self.to_date, changed_at_run_cpr
            )

        logger.info("Number of changed persons", n=len(all_sd_persons_changed))
        real_sd_persons_changed = filter(is_valid_cpr, all_sd_persons_changed)

        # Filter employees based on the sd_cprs list
        sd_cpr_filtered_persons = filter(
            partial(cpr_env_filter, self.settings), real_sd_persons_changed
        )

        sd_persons_changed = map(convert_to_sd_base_person, sd_cpr_filtered_persons)

        sd_persons_iter1, sd_persons_iter2 = tee(sd_persons_changed)
        mo_persons_iter = map(fetch_mo_person, sd_persons_iter2)

        person_pairs = zip(sd_persons_iter1, mo_persons_iter)
        has_mo_person = itemgetter(1)

        new_pairs, current_pairs = partition(has_mo_person, person_pairs)

        # Update the names of the persons already in MO
        for sd_person, mo_person in current_pairs:
            given_name = sd_person.given_name or (
                mo_person.givenname if mo_person.givenname is not None else ""
            )
            surname = sd_person.surname or (
                mo_person.surname if mo_person.surname is not None else ""
            )
            sd_name = f"{sd_person.given_name} {sd_person.surname}"

            uuid = str(mo_person.uuid)
            if mo_person.name != sd_name:
                upsert_employee(str(uuid), given_name, surname, sd_person.cpr)

            if self.settings.sd_phone_number_id_for_ad_creation:
                # Note that we should never remove an "AD-bruger fra SD" IT-system
                # connection once it has been created according to
                # https://redmine.magenta-aps.dk/issues/56089
                employee_it_systems = get_employee_it_systems(
                    self.mo_graphql_client, UUID(uuid)
                )

                sd_to_ad_it_system_uuid = get_sd_to_ad_it_system_uuid(
                    self.mo_graphql_client,
                    self.settings.sd_phone_number_id_for_ad_string,
                )

                employee_it_systems_map = {
                    it_user_system.user_key: it_user_system.uuid
                    for it_user_system in employee_it_systems
                    if it_user_system.uuid == sd_to_ad_it_system_uuid
                }

                emp_with_telephone_number_ids = [
                    emp_tni  # Short for emp_with_telephone_number_identifiers
                    for emp_tni in sd_person.emp_with_telephone_number_identifiers
                    if (
                        self.settings.sd_phone_number_id_trigger
                        in emp_tni.telephone_number_ids
                        and emp_tni.employment_identifier not in employee_it_systems_map
                    )
                ]

                for emp_tni in emp_with_telephone_number_ids:
                    self._create_sd_to_ad_it_system_connection(
                        UUID(uuid), emp_tni.employment_identifier
                    )

        # Create new SD persons in MO
        for sd_person, _ in new_pairs:
            given_name = sd_person.given_name or ""
            surname = sd_person.surname or ""
            logger.info(
                "Create new person",
                given_name=given_name,
                surname=surname,
                cpr=anonymize_cpr(sd_person.cpr),
            )

            sam_account_name, object_guid = self._fetch_ad_information(sd_person.cpr)

            if object_guid:
                uuid = object_guid
                logger.debug("Using ObjectGuid as MO UUID", uuid=uuid)
            else:
                uuid = str(uuid4())
                logger.debug(
                    "User not in MO, UUID list or AD, assigning UUID", uuid=uuid
                )

            return_uuid = upsert_employee(str(uuid), given_name, surname, sd_person.cpr)

            if sam_account_name:
                # Create an IT system for the person If the person is found in the AD
                create_itsystem_connection(sam_account_name, return_uuid)

            if self.settings.sd_phone_number_id_for_ad_creation:
                emp_with_telephone_number_ids = [
                    emp_tni  # Short for emp_with_telephone_number_identifiers
                    for emp_tni in sd_person.emp_with_telephone_number_identifiers
                    if self.settings.sd_phone_number_id_trigger
                    in emp_tni.telephone_number_ids
                ]
                for emp_tni in emp_with_telephone_number_ids:
                    self._create_sd_to_ad_it_system_connection(
                        UUID(str(uuid)), emp_tni.employment_identifier
                    )

    def _compare_dates(self, first_date, second_date, expected_diff=1):
        """
        Return true if the amount of days between second and first is smaller
        than  expected_diff.
        """
        first = datetime.datetime.strptime(first_date, "%Y-%m-%d")
        second = datetime.datetime.strptime(second_date, "%Y-%m-%d")
        delta = second - first
        # compare = first + datetime.timedelta(days=expected_diff)
        compare = abs(delta.days) <= expected_diff
        logger.debug(
            "Compare dates",
            first=first,
            second=second,
            expected_diff=expected_diff,
            compare=compare,
        )
        return compare

    def _refresh_mo_engagements(self, person_uuid):
        self.mo_engagements_cache.pop(person_uuid, None)

    def _fetch_mo_engagements(self, person_uuid):
        if person_uuid in self.mo_engagements_cache:
            return self.mo_engagements_cache[person_uuid]

        mo_engagements = self.helper.read_user_engagement(
            person_uuid, read_all=True, only_primary=True, use_cache=False
        )
        self.mo_engagements_cache[person_uuid] = mo_engagements
        return mo_engagements

    def _find_last_engagement(self, user_key, person_uuid):
        logger.debug("Find engagement", from_date=self.from_date, user_key=user_key)

        mo_engagements = self._fetch_mo_engagements(person_uuid)

        relevant_engagements = filter(
            lambda mo_eng: mo_eng["user_key"] == user_key, mo_engagements
        )
        relevant_engagement = last(relevant_engagements, None)

        if relevant_engagement is None:
            logger.info(
                "Fruitlessly searched for employment_id in engagements",
                user_key=user_key,
                mo_engagements=mo_engagements,
            )
        return relevant_engagement

    def _create_class(self, user_key: str, name: str, facet_uuid: str) -> str:
        """Create a new class

        Returns:
            uuid of the newly created class.
        """
        logger.info(
            "_create_class", user_key=user_key, name=name, facet_uuid=facet_uuid
        )
        mutation = gql(
            """
            mutation CreateClass($input: ClassCreateInput!) {
                class_create(input: $input) {
                    uuid
                }
            }
            """
        )
        response = self.mo_graphql_client.execute(
            mutation,
            variable_values={
                "input": {
                    "name": name,
                    "user_key": user_key,
                    "facet_uuid": facet_uuid,
                    "validity": {"from": "1930-01-01"},
                    "scope": "TEXT",
                }
            },
        )
        logger.info("CreateClass mutation responded", response=response)
        return response["class_create"]["uuid"]

    def _create_engagement_type(self, engagement_type_ref, job_position):
        # Could not fetch, attempt to create it
        logger.warning(
            "Missing engagement_type (now creating)",
            engagement_type_ref=engagement_type_ref,
        )
        engagement_type_uuid = self._create_class(
            engagement_type_ref, job_position, self.engagement_type_facet
        )
        self.engagement_types[engagement_type_ref] = engagement_type_uuid

        self.job_sync.sync_from_sd(job_position, refresh=True)

        return engagement_type_uuid

    def _create_professions(self, job_function, job_position):
        # Could not fetch, attempt to create it
        logger.warning("Missing profession (now creating)", job_function=job_function)
        job_uuid = self._create_class(
            job_function, job_function, self.job_function_facet
        )
        self.job_functions[job_function] = job_uuid

        self.job_sync.sync_from_sd(job_position, refresh=True)

        return job_uuid

    def _fetch_engagement_type(self, job_position):
        """Fetch an engagement type UUID, create if missing.

        Args:
            engagement_type_ref: String of the expected engagement_type name

        Returns:
            uuid of the engagement type or None if it could not be created.
        """
        # Attempt to fetch the engagement type
        engagement_type_ref = "engagement_type" + job_position
        engagement_type_uuid = self.engagement_types.get(engagement_type_ref)
        if engagement_type_uuid:
            return engagement_type_uuid
        return self._create_engagement_type(engagement_type_ref, job_position)

    def _fetch_professions(self, job_function, job_position):
        """Fetch an job function UUID, create if missing.

        This function does not depend on self.settings.sd_job_function, as the
        argument is just a string. If self.settings.sd_job_function is
        JobFunction.job_position_identifier, the string will be the SD
        JobPositionIdentifier, otherwise it will be the actual job name.

        Args:
            emp_name: Overloaded job identifier string / employment name.

        Returns:
            uuid of the job function or None if it could not be created.
        """
        # If hardcoding job title, override the job_function with "Medarbejder"
        if self.settings.sd_job_function is JobFunction.always_medarbejder:
            job_function = "Medarbejder"

        # Add new profssions to LoRa
        job_uuid = self.job_functions.get(job_function)
        if job_uuid:
            return job_uuid
        return self._create_professions(job_function, job_position)

    def create_leave(self, status, user_key, person_uuid: str):
        """Create a leave for a user"""
        logger.info("Create leave", user_key=user_key, status=status)
        # TODO: This code potentially creates duplicated leaves.

        # Notice, the expected and desired behaviour for leaves is for the engagement
        # to continue during the leave. It turns out this is actually what happens
        # because a leave is apparently always accompanied by a worktime-update that
        # forces an edit to the engagement that will extend it to span the
        # leave. If this ever turns out not to hold, add a dummy-edit to the
        # engagement here.
        mo_eng = self._find_last_engagement(user_key, person_uuid)
        payload = sd_payloads.create_leave(
            mo_eng,
            person_uuid,
            str(self.leave_uuid),
            user_key,
            sd_to_mo_validity(status),
        )

        logger.debug("Create leave (details/create)", payload=payload)
        if not self.dry_run:
            response = self.helper._mo_post("details/create", payload)
            assert response.status_code == 201

    def create_association(self, department, person_uuid, user_key, validity):
        """Create a association for a user"""
        logger.info("Consider to create an association")
        associations = self.helper.read_user_association(
            person_uuid, read_all=True, only_primary=True
        )
        logger.debug("Associations read from MO", associations=associations)
        hit = False
        for association in associations:
            if (
                association["validity"] == validity
                and association["org_unit"]["uuid"] == department
            ):
                hit = True
        if not hit:
            logger.info("Association needs to be created")
            payload = sd_payloads.create_association(
                department,
                person_uuid,
                str(self.association_uuid),
                user_key,
                validity,
            )
            logger.debug("Create association (details/create)", payload=payload)
            if not self.dry_run:
                response = self.helper._mo_post("details/create", payload)
                assert response.status_code == 201
        else:
            logger.info("No new Association is needed")

    def apply_NY_logic(self, org_unit, user_key, validity, person_uuid) -> str:
        logger.debug(
            "Apply NY logic",
            user_key=user_key,
            org_unit=org_unit,
            validity=validity,
        )
        too_deep = self.settings.sd_import_too_deep

        # Effective date for fixing the department must not be before today's date,
        # since we cannot get SD department parents *with a validity* back in time via
        # the SD API. It is only possible to get an SD department parent on a specific
        # date.
        # Calling fix_department with a date argument prior to today's date can
        # therefore result in an incorrect parent (see more details on this Redmine
        # issue https://redmine.magenta.dk/issues/58094)
        validity_from_date = parse_datetime(validity["from"]).date()
        effective_fix_date = max(validity_from_date, datetime.datetime.now().date())
        effective_fix_date_str = format_date(effective_fix_date)

        # Move users and make associations according to NY logic
        ou_info = self.helper.read_ou(
            org_unit, at=effective_fix_date_str, use_cache=False
        )
        if "status" in ou_info:
            self.department_fixer.fix_department(org_unit, effective_fix_date)
            ou_info = self.helper.read_ou(
                org_unit, at=effective_fix_date_str, use_cache=False
            )

        if ou_info["org_unit_level"]["user_key"] in too_deep:
            self.create_association(org_unit, person_uuid, user_key, validity)

        while ou_info["org_unit_level"]["user_key"] in too_deep:
            ou_info = ou_info["parent"]
            logger.debug("Parent unit", uuid=ou_info["uuid"])
        org_unit = ou_info["uuid"]

        return org_unit

    def create_new_engagement(self, sd_employment, status, cpr, person_uuid):
        """
        Create a new engagement
        AD integration handled in check for primary engagement.
        """
        # beware - name engagement_info used for engagement in engagement_components

        assert (
            EmploymentStatus(status["EmploymentStatusCode"])
            not in EmploymentStatus.let_go()
        )

        sd_emp_id, engagement_info = engagement_components(sd_employment)
        user_key = get_eng_user_key(
            sd_emp_id,
            self.current_inst_id,
            self.settings.sd_prefix_eng_user_key_with_inst_id,
        )
        if not engagement_info["departments"] or not engagement_info["professions"]:
            return False

        # TODO: This assumption is problematic since there may be more than
        # one element in the professions list
        job_position = engagement_info["professions"][0]["JobPositionIdentifier"]

        validity = sd_to_mo_validity(status)
        also_edit = False
        if (
            len(engagement_info["professions"]) > 1
            or len(engagement_info["working_time"]) > 1
            or len(engagement_info["departments"]) > 1
        ):
            also_edit = True
        logger.debug("Create new engagement", also_edit=also_edit)

        try:
            org_unit = engagement_info["departments"][0]["DepartmentUUIDIdentifier"]
            logger.info("Org unit for new engagement", org_unit=org_unit)
            org_unit = self.apply_NY_logic(org_unit, user_key, validity, person_uuid)
        except IndexError:
            msg = "No unit for engagement {}".format(user_key)
            logger.error(msg)
            raise Exception(msg)

        try:
            emp_name = engagement_info["professions"][0]["EmploymentName"]
        except (KeyError, IndexError):
            emp_name = "Ukendt"

        job_function = emp_name
        if self.settings.sd_job_function is JobFunction.job_position_identifier:
            job_function = job_position

        engagement_type = self.determine_engagement_type(sd_employment, job_position)
        if engagement_type is None:
            return False

        extension_field = self.settings.sd_employment_field
        extension = {}
        if extension_field is not None:
            extension = {extension_field: emp_name}

        job_function_uuid = self._fetch_professions(job_function, job_position)

        payload = sd_payloads.create_engagement(
            org_unit=org_unit,
            person_uuid=person_uuid,
            job_function=job_function_uuid,
            engagement_type=engagement_type,
            user_key=user_key,
            engagement_info=engagement_info,
            validity=validity,
            **extension,
        )

        logger.debug("Create engagement (details/create)", payload=payload)
        if not self.dry_run:
            response = self.helper._mo_post("details/create", payload)
            assert response.status_code == 201

        self._refresh_mo_engagements(person_uuid)
        logger.info("Engagement created", user_key=user_key)

        if also_edit:
            # This will take of the extra entries
            self.edit_engagement(sd_employment, person_uuid, cpr)

        return True

    def _terminate_engagement(
        self,
        user_key: str,
        person_uuid: str,  # TODO: change type to UUID
        from_date: str,  # TODO: Introduce MO date version
        to_date: str | None = None,
    ) -> bool:
        """
        Terminate an employment (engagement) in MO. Since this function calls
        MO, the parameters are adapted to accommodate MO instead of SD Løn,
        i.e. SD dates should be converted to MO dates before the function is
        invoked.

        Args:
            user_key: SD Løn employment ID. Used as BVN (user_key) in MO.
            person_uuid: The employee UUID in MO.
            from_date: The MO "from" date (to be set in virkning).
            to_date: The MO "to" date (to be set in virkning).

        Returns:
            `True` if the termination in MO was successful and `False`
            otherwise
        """
        logger.info(
            "Terminate engagement",
            user_key=user_key,
            person_uuid=person_uuid,
            from_date=from_date,
            to_date=to_date,
        )
        mo_engagement = self._find_last_engagement(user_key, person_uuid)

        if not mo_engagement:
            logger.warning("Terminating non-existing job!", user_key=user_key)
            return False

        terminate_eng_from_uuid(
            self.helper, mo_engagement["uuid"], self.dry_run, from_date, to_date
        )
        self._refresh_mo_engagements(person_uuid)

        return True

    def edit_engagement_department(self, sd_employment, mo_eng, person_uuid):
        # This function may cause incorrect data in MO, since mo_eng is only the latest
        # engagement in MO. We should instead loop over all (GraphQL) engagement
        # validities and update each validity interval one at a time.
        employment_id, engagement_info = engagement_components(sd_employment)
        user_key = get_eng_user_key(
            employment_id,
            self.current_inst_id,
            self.settings.sd_prefix_eng_user_key_with_inst_id,
        )

        for department in engagement_info["departments"]:
            logger.info("Change department of engagement", user_key=user_key)
            logger.debug("Department object", department=department)

            validity = sd_to_mo_validity(department)

            logger.debug("Validity of this department change", validity=validity)
            org_unit = department["DepartmentUUIDIdentifier"]
            if org_unit is None:
                logger.warning(
                    "DepartmentUUIDIdentifier was None, attempting GetDepartment"
                )
                # This code should not be necessary, but SD returns bad data.
                # Sometimes the UUID is missing, even if it can be looked up?
                url = "GetDepartment20111201"
                params = {
                    "ActivationDate": self.from_date.strftime("%d.%m.%Y"),
                    "DeactivationDate": self.from_date.strftime("%d.%m.%Y"),
                    "DepartmentNameIndicator": "true",
                    "UUIDIndicator": "true",
                    "DepartmentIdentifier": department["DepartmentIdentifier"],
                }
                request_uuid = uuid.uuid4()
                logger.info("edit_engagement_department", request_uuid=request_uuid)
                response = sd_lookup(
                    url,
                    settings=self.settings,
                    params=params,
                    request_uuid=request_uuid,
                    dry_run=self.dry_run,
                    institution_identifier=self.current_inst_id,
                )
                logger.warning("GetDepartment returned", response=response)
                org_unit = response["Department"]["DepartmentUUIDIdentifier"]
                if org_unit is None:
                    logger.fatal("DepartmentUUIDIdentifier was None inside failover.")
                    sys.exit(1)

            associations = self.helper.read_user_association(person_uuid, read_all=True)
            logger.debug("User associations", associations=associations)
            current_association = None
            # TODO: This is a filter + next (only?)
            for association in associations:
                if association["user_key"] == user_key:
                    current_association = association["uuid"]

            if current_association:
                logger.debug("We need to move", current_association=current_association)
                data = {"org_unit": {"uuid": org_unit}, "validity": validity}
                payload = sd_payloads.association(data, current_association)
                logger.debug("Association edit payload (details/edit)", payload=payload)
                if not self.dry_run:
                    response = self.helper._mo_post("details/edit", payload)
                    mora_assert(response)

            org_unit = self.apply_NY_logic(org_unit, user_key, validity, person_uuid)

            logger.debug("New org unit for edited engagement", org_unit=org_unit)
            data = {"org_unit": {"uuid": org_unit}, "validity": validity}
            payload = sd_payloads.engagement(data, mo_eng)
            logger.debug("Edit engagement org unit (details/edit)", payload=payload)
            if not self.dry_run:
                response = self.helper._mo_post("details/edit", payload)
                mora_assert(response)

            re_terminate_engagement(
                self.helper,
                mo_eng,
                department,
                engagement_info["status_list"],
                self.dry_run,
            )

    def determine_engagement_type(self, sd_employment, job_position):
        split = self.settings.sd_monthly_hourly_divide
        employment_id = calc_employment_id(sd_employment)
        if employment_id["value"] < split:
            return self.engagement_types.get("månedsløn")
        # XXX: Is the first condition not implied by not hitting the above case?
        if (split - 1) < employment_id["value"] < 999999:
            return self.engagement_types.get("timeløn")
        # This happens if EmploymentID is not a number
        # XXX: Why are we checking against 999999 instead of checking the type?
        # Once we get here, we know that it is a no-salary employee

        # We should not create engagements (or engagement_types) for engagements
        # with too low of a job_position id compared to no_salary_minimum_id.
        if (
            self.no_salary_minimum is not None
            and int(job_position) < self.no_salary_minimum
        ):
            logger.warning("No salary employee, with too low job_position id")
            return None

        # We need a special engagement type for the engagement.
        # We will try to fetch and try to create it if we cannot find it.
        logger.info("Non-numeric id. Job pos id", job_position_id=job_position)
        return self._fetch_engagement_type(job_position)

    def edit_engagement_type(self, sd_employment, mo_eng):
        # This function may cause incorrect data in MO, since mo_eng is only the latest
        # engagement in MO. We should instead loop over all (GraphQL) engagement
        # validities and update each validity interval one at a time.
        employment_id, engagement_info = engagement_components(sd_employment)
        for profession_info in engagement_info["professions"]:
            logger.info(
                "Change engagement type of engagement", employment_id=employment_id
            )
            job_position = profession_info["JobPositionIdentifier"]

            validity = sd_to_mo_validity(profession_info)

            engagement_type = self.determine_engagement_type(
                sd_employment, job_position
            )
            if engagement_type is None:
                continue
            data = {"engagement_type": {"uuid": engagement_type}, "validity": validity}
            payload = sd_payloads.engagement(data, mo_eng)
            logger.debug(
                "Update engagement type payload (details/edit)", payload=payload
            )
            if not self.dry_run:
                response = self.helper._mo_post("details/edit", payload)
                mora_assert(response)

            re_terminate_engagement(
                self.helper,
                mo_eng,
                profession_info,
                engagement_info["status_list"],
                self.dry_run,
            )

    def edit_engagement_profession(self, sd_employment, mo_eng):
        # This function may cause incorrect data in MO, since mo_eng is only the latest
        # engagement in MO. We should instead loop over all (GraphQL) engagement
        # validities and update each validity interval one at a time.
        employment_id, engagement_info = engagement_components(sd_employment)
        for profession_info in engagement_info["professions"]:
            logger.info("Change profession of engagement", employment_id=employment_id)
            job_position = profession_info["JobPositionIdentifier"]

            # The variability handling introduced in the following lines
            # (based on the value of job_position) is not optimal, i.e.
            # a parametric if-switch is used, where a strategy pattern would
            # be more appropriate. However, due to all the hard couplings in
            # the code, a strategy pattern is not feasible for now. Let's
            # leave it as is until the whole SD code base is rewritten

            if not is_employment_id_and_no_salary_minimum_consistent(
                sd_employment, self.no_salary_minimum
            ):
                sd_from_date = profession_info["ActivationDate"]
                sd_to_date = profession_info["DeactivationDate"]
                self._terminate_engagement(
                    mo_eng["user_key"],
                    mo_eng["person"]["uuid"],
                    sd_from_date,
                    sd_to_mo_date(sd_to_date),
                )
            else:
                emp_name = profession_info.get("EmploymentName", job_position)
                validity = sd_to_mo_validity(profession_info)

                job_function = emp_name
                if self.settings.sd_job_function is JobFunction.job_position_identifier:
                    job_function = job_position
                logger.debug("Employment name", job_function=job_function)

                ext_field = self.settings.sd_employment_field
                extention = {}
                if ext_field is not None:
                    extention = {ext_field: emp_name}

                job_function_uuid = self._fetch_professions(job_function, job_position)

                data = {
                    "job_function": {"uuid": job_function_uuid},
                    "validity": validity,
                }
                data.update(extention)
                payload = sd_payloads.engagement(data, mo_eng)

                logger.debug(
                    "Update profession payload (details/edit)", payload=payload
                )
                if not self.dry_run:
                    response = self.helper._mo_post("details/edit", payload)
                    mora_assert(response)

                re_terminate_engagement(
                    self.helper,
                    mo_eng,
                    profession_info,
                    engagement_info["status_list"],
                    self.dry_run,
                )

    def edit_engagement_worktime(self, sd_employment, mo_eng):
        # This function may cause incorrect data in MO, since mo_eng is only the latest
        # engagement in MO. We should instead loop over all (GraphQL) engagement
        # validities and update each validity interval one at a time.
        employment_id, engagement_info = engagement_components(sd_employment)
        for worktime_info in engagement_info["working_time"]:
            logger.info(
                "Change working time of engagement", employment_id=employment_id
            )

            validity = sd_to_mo_validity(worktime_info)

            working_time = float(worktime_info["OccupationRate"])
            data = {"fraction": int(working_time * 1000000), "validity": validity}
            payload = sd_payloads.engagement(data, mo_eng)
            logger.debug("Change worktime payload (details/edit)", payload=payload)
            if not self.dry_run:
                response = self.helper._mo_post("details/edit", payload)
                mora_assert(response)

            re_terminate_engagement(
                self.helper,
                mo_eng,
                worktime_info,
                engagement_info["status_list"],
                self.dry_run,
            )

    def edit_engagement_status(
        self, status_list: list[dict[str, str]], mo_eng: dict[str, Any]
    ) -> None:
        """
        Extend the engagement validity in MO if the active engagement status
        have been extended in SD.

        NOTE: the logic below is not guaranteed to work for complex SD payloads!

        Args:
            status_list: a list of the EmploymentStatus objects from the SD payload.
            mo_eng: the MO engagement
        """

        latest_active_sd_date_str = last(
            emp
            for emp in status_list
            if EmploymentStatus(emp["EmploymentStatusCode"])
            in EmploymentStatus.employeed()
        )["DeactivationDate"]
        latest_active_sd_date = parse_datetime(latest_active_sd_date_str).date()

        latest_active_mo_date_str: str | None = mo_eng["validity"]["to"]
        latest_active_mo_date = (
            parse_datetime(latest_active_mo_date_str).date()
            if latest_active_mo_date_str is not None
            else datetime.date.max
        )

        if latest_active_sd_date > latest_active_mo_date:
            payload = sd_payloads.engagement(
                {
                    # The user_key is a random choice - we could just as well have
                    # updated something else
                    "user_key": mo_eng["user_key"],
                    "validity": {
                        "from": mo_eng["validity"]["from"],
                        "to": sd_to_mo_date(latest_active_sd_date_str),
                    },
                },
                mo_eng,
            )
            logger.info(
                "Update MO engagement status end date",
                latest_active_sd_date=latest_active_sd_date_str,
                latest_active_mo_date=latest_active_mo_date_str,
                payload=payload,
            )
            if not self.dry_run:
                response = self.helper._mo_post("details/edit", payload)
                mora_assert(response)

    def edit_engagement(self, sd_employment, person_uuid, cpr: str):
        """
        Edit an engagement
        """
        employment_id, eng_components = engagement_components(sd_employment)
        user_key = get_eng_user_key(
            employment_id,
            self.current_inst_id,
            self.settings.sd_prefix_eng_user_key_with_inst_id,
        )

        logger.debug("Edit engagement", user_key=user_key, person_uuid=person_uuid)
        mo_eng = self._find_last_engagement(user_key, person_uuid)

        employment_consistent = is_employment_id_and_no_salary_minimum_consistent(
            sd_employment, self.no_salary_minimum
        )

        if mo_eng is None:
            if employment_consistent:
                # The engagement does not exist yet. This can happen (e.g.) if an
                # engagement was not initially created due to a too low
                # JobPositionIdentifier, but the JobPositionIdentifier is later changed
                # to a sufficiently large value. In this case we need to look up the
                # full engagement at a proper date.

                sd_lookup_date = create_eng_lookup_date(eng_components)

                create_engagement(self, employment_id, person_uuid, cpr, sd_lookup_date)
            return

        update_existing_engagement(self, mo_eng, sd_employment, person_uuid)

    def _handle_employment_status_changes(
        self, cpr: str, sd_employment: OrderedDict, person_uuid: str
    ) -> bool:
        # NOTE: the logic in this function and its calling function is buggy and does
        # not guarantee that we obtain the correct state in MO. The whole thing needs
        # to be rewritten.

        """
        Update MO with SD employment changes.

        Args:
            cpr: The CPR number of the person.
            sd_employment: The SD employment (see example below).
            person_uuid: The UUID of the MO employee.

        Returns:
            `True` if further engagement editing should be skipped
            `False` otherwise.

        Examples:
            The sd_employment could for example look like this:
                ```python
                OrderedDict([
                    ('EmploymentIdentifier', '12345'),
                    ('EmploymentDate', '2020-11-10'),
                    ('EmploymentDepartment', OrderedDict([
                        ('@changedAtDate', '2020-11-10'),
                        ('ActivationDate', '2020-11-10'),
                        ('ActivationTime', '06:00'),
                        ('DeactivationDate', '9999-12-31'),
                        ('DepartmentIdentifier', 'department_id'),
                        ('DepartmentUUIDIdentifier', 'department_uuid')
                    ])),
                    ('Profession', OrderedDict([
                        ('@changedAtDate', '2020-11-10'),
                        ('ActivationDate', '2020-11-10'),
                        ('ActivationTime', '06:00'),
                        ('DeactivationDate', '9999-12-31'),
                        ('JobPositionIdentifier', '1'),
                        ('EmploymentName', 'chief'),
                        ('AppointmentCode', '0')
                    ])),
                    ('EmploymentStatus', [
                        OrderedDict([
                            ('@changedAtDate', '2020-11-10'),
                            ('ActivationDate', '2020-11-10'),
                            ('ActivationTime', '06:00'),
                            ('DeactivationDate', '2021-02-09'),
                            ('EmploymentStatusCode', '1')
                        ]),
                        OrderedDict([
                            ('@changedAtDate', '2020-11-10'),
                            ('ActivationDate', '2021-02-10'),
                            ('ActivationTime', '06:00'),
                            ('DeactivationDate', '9999-12-31'),
                            ('EmploymentStatusCode', '8')
                        ])
                    ])
                ])
                ```
        """

        skip = False
        # The EmploymentStatusCode can take a number of magical values.
        # that must be handled separately.
        employment_id, eng = engagement_components(sd_employment)
        user_key = get_eng_user_key(
            employment_id,
            self.current_inst_id,
            self.settings.sd_prefix_eng_user_key_with_inst_id,
        )

        logger.info(
            "Handle employment status changes",
            emp_id=employment_id,
            user_key=user_key,
            cpr=anonymize_cpr(cpr),
        )

        for status in eng["status_list"]:
            logger.info("EmploymentStatus", emp_status=status)
            code = status["EmploymentStatusCode"]
            code = EmploymentStatus(code)

            if code in [EmploymentStatus.AnsatUdenLoen, EmploymentStatus.AnsatMedLoen]:
                mo_eng = self._find_last_engagement(user_key, person_uuid)
                if mo_eng:
                    logger.info("Found MO engagement", eng_uuid=mo_eng["uuid"])
                    self._refresh_mo_engagements(person_uuid)
                    self.edit_engagement_status(eng["status_list"], mo_eng)
                    self.edit_engagement(sd_employment, person_uuid, cpr)
                else:
                    logger.info("MO engagement not found. Create new engagement")
                    if is_employment_id_and_no_salary_minimum_consistent(
                        sd_employment, self.no_salary_minimum
                    ):
                        self.create_new_engagement(
                            sd_employment, status, cpr, person_uuid
                        )
                skip = True
            elif code == EmploymentStatus.Orlov:
                mo_eng = self._find_last_engagement(user_key, person_uuid)
                if not mo_eng:
                    if self.settings.sd_skip_leave_creation_if_no_engagement:
                        logger.info("Not allowed to create leave with no engagement")
                        continue
                    logger.info("Leave for non existent eng., create one")
                    if is_employment_id_and_no_salary_minimum_consistent(
                        sd_employment, self.no_salary_minimum
                    ):
                        self.create_new_engagement(
                            sd_employment, status, cpr, person_uuid
                        )
                logger.info("Create a leave")
                self.create_leave(status, user_key, person_uuid)
            elif code in EmploymentStatus.let_go():
                mo_eng = self._find_last_engagement(user_key, person_uuid)
                if not mo_eng:
                    logger.info(
                        "Could not find MO engagement for passive SD employment. "
                        "Skipping further processing",
                        code=code,
                    )
                    skip = True
                    # This break could cause problems if there are further statuses
                    # following the status 8, but it is, however, unlikely that this
                    # will happen.
                    break

                sd_from_date = status["ActivationDate"]
                sd_to_date = status["DeactivationDate"]
                success = self._terminate_engagement(
                    user_key=user_key,
                    person_uuid=person_uuid,
                    from_date=sd_from_date,
                    to_date=sd_to_mo_date(sd_to_date),
                )
                if not success:
                    logger.error("Problem terminating employment", user_key=user_key)
            elif code == EmploymentStatus.Slettet:
                # TODO: rename user_key to something unique in MO when employee
                # is terminated.
                #
                # The reason for this is that SD Løn sometimes reuses the same
                # employment_id for *different* persons, e.g. if a person with
                # employment_id=12345 is set to "Slettet" then a different newly
                # employed SD person can get the SAME employment_id!
                #
                # In MO we therefore have to do the following. When a MO person
                # is terminated, we have to make sure the the user_key (BVN) of
                # that user is changed to some unique, e.g. "old user_key +
                # UUID (or date)". In that way we can avoid user_key conflicts
                # between different employees.
                #
                # Note that an SD person can jump from any status to "Slettet"

                for mo_eng in self._fetch_mo_engagements(person_uuid):
                    if mo_eng["user_key"] == user_key:
                        sd_from_date = status["ActivationDate"]
                        self._terminate_engagement(
                            user_key=user_key,
                            person_uuid=person_uuid,
                            from_date=sd_from_date,
                        )
        return skip

    def _update_user_employments(
        self, cpr: str, sd_employments, person_uuid: str
    ) -> None:
        for sd_employment in sd_employments:
            employment_id, eng = engagement_components(sd_employment)
            logger.debug(
                "Update SD employment",
                cpr=anonymize_cpr(cpr),
                employment_id=employment_id,
                employmentsd_employment=sd_employment,
            )
            # If status is present, we have a potential creation
            if eng["status_list"] and self._handle_employment_status_changes(
                cpr, sd_employment, person_uuid
            ):
                continue
            self.edit_engagement(sd_employment, person_uuid, cpr)

    def update_all_employments(self, in_cpr: Optional[str] = None) -> None:
        if in_cpr is not None:
            employments_changed = self.read_employment_changed(in_cpr=in_cpr)
        else:
            logger.info("Update all employments")
            employments_changed = self.read_employment_changed()

        logger.info("Number of employments to update", n=len(employments_changed))

        employments_changed = filter(is_valid_cpr, employments_changed)

        # Filter employees based on the sd_cprs list
        employments_changed = filter(
            partial(cpr_env_filter, self.settings), employments_changed
        )

        recalculate_users: Set[UUID] = set()

        for employment in employments_changed:
            cpr = employment["PersonCivilRegistrationIdentifier"]
            sd_employments = ensure_list(employment["Employment"])
            sd_employments = [
                filtered_professions(employment, self.settings.sd_skip_employment_types)
                for employment in sd_employments
            ]

            logger.info(30 * "#")
            logger.info("Update employment", cpr=anonymize_cpr(cpr))
            logger.info(30 * "#")

            mo_person = self.helper.read_user(user_cpr=cpr, org_uuid=self.org_uuid)
            # Person not in MO, but they should be
            if not mo_person:
                logger.warning("This person should be in MO, but is not")
                try:
                    self.update_changed_persons(in_cpr=cpr)
                    mo_person = self.helper.read_user(
                        user_cpr=cpr, org_uuid=self.org_uuid
                    )
                except Exception as exp:
                    logger.error("Unable to find person in MO", err=exp)
                    continue

            if not mo_person:
                logger.warning("MO person not set!!")
                continue
            person_uuid = mo_person["uuid"]

            self._refresh_mo_engagements(person_uuid)
            self._update_user_employments(cpr, sd_employments, person_uuid)

            # Re-calculate primary after all updates for user has been performed.
            recalculate_users.add(person_uuid)


def initialize_changed_at(from_date):
    persist_status(from_date, from_date, RunDBState.RUNNING)
    settings = get_settings()
    inst_ids = ensure_list(settings.sd_institution_identifier)

    logger.info("Start initialization run")

    for inst_id in inst_ids:
        logger.info("Start initial ChangedAt", inst_id=inst_id)
        sd_updater = ChangeAtSD(settings, inst_id, from_date)
        sd_updater.update_changed_persons()
        sd_updater.update_all_employments()
        logger.info("Ended initial ChangedAt", inst_id=inst_id)

    persist_status(from_date, from_date, RunDBState.COMPLETED)

    logger.info("Finished initialization run")


@click.group()
def cli():
    pass


@cli.command()
def changed_at_cli():
    """Tool to delta synchronize with MO with SD."""
    changed_at(dipex_last_success_timestamp, sd_changed_at_state)


@cli.command()
def changed_at_init():
    """SD-changed-at initialization"""
    logger.info("Starting SD-changed-at initialization")

    settings = get_settings()
    setup_logging(
        settings.log_level,
        settings.log_to_file,
        settings.log_file,
        settings.log_file_backup_count,
    )

    from_date = date_to_datetime(settings.sd_global_from_date)
    from_date = from_date.astimezone(tz=datetime.timezone.utc)

    initialize_changed_at(from_date)


def changed_at(
    dipex_last_success_timestamp: Gauge,
    sd_changed_at_state: Enum,
):
    """Tool to delta synchronize with MO with SD."""
    settings = get_settings()
    setup_logging(
        settings.log_level,
        settings.log_to_file,
        settings.log_file,
        settings.log_file_backup_count,
    )

    logger.info("Program started")

    inst_ids = ensure_list(settings.sd_institution_identifier)

    run_db_state = get_status()
    logger.info("The RunDB state is", run_db_state=run_db_state)

    sd_changed_at_state.state(run_db_state.value)
    if not run_db_state == RunDBState.COMPLETED:
        logger.error(
            "Previous run did not complete or RunDB state is unknown!",
            run_db_state=run_db_state,
        )
        raise PreviousRunNotCompletedError()
    sd_changed_at_state.state(RunDBState.RUNNING.value)

    from_date = get_run_db_from_date()
    to_date = datetime.datetime.now(tz=ZoneInfo("Europe/Copenhagen"))
    dates = gen_date_intervals(from_date, to_date)
    for from_date, to_date in dates:
        persist_status(from_date, to_date, RunDBState.RUNNING)

        for inst_id in inst_ids:
            logger.info(
                "Initialize ChangedAtSD class",
                from_date=from_date,
                to_date=to_date,
                inst_id=inst_id,
            )
            sd_updater = ChangeAtSD(settings, inst_id, from_date, to_date)  # type: ignore

            logger.info("Update changed persons")
            sd_updater.update_changed_persons()

            logger.info("Update all employments")
            sd_updater.update_all_employments()

        persist_status(from_date, to_date, RunDBState.COMPLETED)

    dipex_last_success_timestamp.set_to_current_time()
    sd_changed_at_state.state(RunDBState.COMPLETED.value)

    logger.info("Program finished")


@cli.command()
@click.option(
    "--cpr",
    required=True,
    type=click.STRING,
    help="CPR number of the person to import",
)
@click.option(
    "--from-date",
    type=click.DateTime(),
    required=True,
    help="Global import from-date",
)
@click.option(
    "--dry-run", is_flag=True, default=False, help="Dry-run making no actual changes."
)
@click.option(
    "--institution-identifier",
    default=None,
    help="The SD InstitutionIdentifier",
)
def import_single_user(
    cpr: str,
    from_date: datetime.datetime,
    dry_run: bool,
    institution_identifier: str | None,
):
    """Import a single user into MO."""

    settings = get_settings()

    if institution_identifier is None:
        assert isinstance(settings.sd_institution_identifier, str)
        inst_id = settings.sd_institution_identifier
    else:
        inst_id = institution_identifier

    sd_updater = ChangeAtSD(settings, inst_id, from_date, None, dry_run)

    sd_updater.update_changed_persons(cpr)
    sd_updater.update_all_employments(cpr)


@cli.command()
@click.option(
    "--from-date",
    type=click.DateTime(),
    required=True,
    help="The start date to run from",
)
@click.option(
    "--to-date", type=click.DateTime(), required=True, help="The end date to run to"
)
@click.option("--cpr", help="CPR as 10 digits if the run should be for a single user")
@click.option(
    "--dry-run", is_flag=True, help="If flag is set, no changes will be made to MO"
)
@click.option(
    "--institution-identifier",
    default=None,
    help="The SD InstitutionIdentifier",
)
def date_interval_run(
    from_date: datetime.datetime,
    to_date: datetime.datetime,
    cpr: str,
    dry_run: bool,
    institution_identifier: str | None,
):
    settings = get_settings()
    setup_logging(
        settings.log_level,
        settings.log_to_file,
        settings.log_file,
        settings.log_file_backup_count,
    )

    logger.info("Date interval run started")

    if institution_identifier is None:
        assert isinstance(settings.sd_institution_identifier, str)
        inst_id = settings.sd_institution_identifier
    else:
        inst_id = institution_identifier

    sd_updater = ChangeAtSD(settings, inst_id, from_date, to_date, dry_run)  # type: ignore

    logger.info("Update changed persons")
    sd_updater.update_changed_persons(changed_at_run_cpr=cpr)

    logger.info("Update all employments")
    sd_updater.update_all_employments(in_cpr=cpr)

    logger.info("Date interval run finished")


if __name__ == "__main__":
    cli()
