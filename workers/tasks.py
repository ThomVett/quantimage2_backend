import os
import shutil
import tempfile
import json
import traceback

import jsonpickle
import requests

import warnings

from flask_socketio import SocketIO
from keycloak.realm import KeycloakRealm
from requests_toolbelt import NonMultipartContentTypeException

from imaginebackend_common.flask_init import create_app
from imaginebackend_common.models import FeatureExtractionTask, db
from imaginebackend_common.kheops_utils import get_token_header, dicomFields
from imaginebackend_common.utils import (
    get_socketio_body_feature_task,
    MessageType,
    task_status_message,
    fetch_extraction_result,
    send_extraction_status_message,
)

warnings.filterwarnings("ignore", message="Failed to parse headers")

from celery import Celery
from celery import states as celerystates
from requests_toolbelt.multipart import decoder
from pathlib import Path

from imaginebackend_common.feature_backends import feature_backends_map, FeatureBackend
from imaginebackend_common.kheops_utils import endpoints

celery = Celery(
    "tasks",
    backend=os.environ["CELERY_RESULT_BACKEND"],
    broker=os.environ["CELERY_BROKER_URL"],
)

# Backend client
realm = KeycloakRealm(
    server_url=os.environ["KEYCLOAK_BASE_URL"],
    realm_name=os.environ["KEYCLOAK_REALM_NAME"],
)
oidc_client = realm.open_id_connect(
    client_id=os.environ["KEYCLOAK_KHEOPS_AUTH_CLIENT_ID"],
    client_secret=os.environ["KEYCLOAK_KHEOPS_AUTH_CLIENT_SECRET"],
)

socketio = SocketIO(message_queue=os.environ["SOCKET_MESSAGE_QUEUE"])

flask_app = create_app()
flask_app.app_context().push()


@celery.task(name="imaginetasks.extract", bind=True)
def run_extraction(
    self,
    feature_extraction_id,
    user_id,
    feature_extraction_task_id,
    study_uid,
    features_path,
    config_path,
):
    try:

        current_step = 1
        steps = 3

        db.session.commit()

        # TODO - Remove this at the end
        # all_tasks = FeatureExtractionTask.query.order_by(
        #     db.desc(FeatureExtractionTask.id)
        # ).all()
        # print(f"There are {len(all_tasks)} tasks in the table")
        # print(f"The latest task has id {all_tasks[0].id}")

        # Affect celery task ID to task (if needed)
        print(f"Getting Feature Extraction Task with id {feature_extraction_task_id}")
        feature_extraction_task = FeatureExtractionTask.find_by_id(
            feature_extraction_task_id
        )

        if not feature_extraction_task:
            raise Exception("Didn't find the task in the DB!!!")

        if not feature_extraction_task.task_id:
            feature_extraction_task.task_id = self.request.id
            feature_extraction_task.save_to_db()

        # Status update - DOWNLOAD
        status_message = "Fetching DICOM files"
        update_progress(
            self,
            feature_extraction_id,
            feature_extraction_task_id,
            current_step,
            steps,
            status_message,
        )

        # Get a token for the given user (possible thanks to token exchange in Keycloak)
        token = oidc_client.token_exchange(
            requested_token_type="urn:ietf:params:oauth:token-type:access_token",
            audience=os.environ["KEYCLOAK_KHEOPS_AUTH_CLIENT_ID"],
            requested_subject=user_id,
        )["access_token"]

        # Get the list of instance URLs
        url_list = get_dicom_url_list(token, study_uid)

        # Download all the DICOM files
        dicom_dir = download_dicom_urls(token, url_list)

        features = extract_all_features(
            self,
            dicom_dir,
            config_path,
            feature_extraction_id,
            feature_extraction_task_id=feature_extraction_task_id,
            current_step=current_step,
            steps=steps,
        )

        # Delete download DIR
        shutil.rmtree(dicom_dir, True)

        # Save the features
        json_features = jsonpickle.encode(features)
        os.makedirs(os.path.dirname(features_path), exist_ok=True)
        Path(features_path).write_text(json_features)

        # Extraction is complete
        status_message = "Extraction Complete"

        # Check if parent is done - TODO Find a better way to do this, not from the task level hopefully
        extraction_status = fetch_extraction_result(
            celery, feature_extraction_task.feature_extraction.result_id
        )

        # TODO - Remove this
        print("PARENT STATUS : ")
        print(vars(extraction_status))

        return {
            "feature_extraction_task_id": feature_extraction_task_id,
            "current": steps,
            "total": steps,
            "completed": steps,
            "status_message": status_message,
        }
    except Exception as e:

        # logging.error(e)

        current_step = 0
        status_message = "Failure!"
        print(
            f"Feature extraction task {feature_extraction_task_id} - {current_step}/{steps} - {status_message}"
        )

        meta = {
            "exc_type": type(e).__name__,
            "exc_message": traceback.format_exc().split("\n"),
            "feature_extraction_task_id": feature_extraction_task_id,
            "current": current_step,
            "total": steps,
            "status_message": status_message,
        }

        update_task_state(self, "FAILURE", meta)

        raise e

    finally:
        db.session.remove()


@celery.task(name="imaginetasks.finalize_extraction", bind=True)
def finalize_extraction(task, results, feature_extraction_id):
    send_extraction_status_message(
        feature_extraction_id, celery, socketio, send_extraction=True
    )
    db.session.remove()


@celery.task(name="imaginetasks.finalize_extraction_task", bind=True)
def finalize_extraction_task(
    task, result, feature_extraction_id, feature_extraction_task_id
):
    # Send Socket.IO message
    socketio_body = get_socketio_body_feature_task(
        task.request.id,
        feature_extraction_task_id,
        celerystates.SUCCESS,
        "Extraction Complete",
    )

    # Send Socket.IO message to clients about task
    socketio.emit(MessageType.FEATURE_TASK_STATUS.value, socketio_body)

    # Send Socket.IO message to clients about extraction
    send_extraction_status_message(feature_extraction_id, celery, socketio)

    db.session.remove()


def update_progress(
    task,
    feature_extraction_id,
    feature_extraction_task_id,
    current_step,
    steps,
    status_message,
):
    print(
        f"Feature extraction task {feature_extraction_task_id} - {current_step}/{steps} - {status_message}"
    )

    status = "PROGRESS"

    meta = {
        "task_id": task.request.id,
        "feature_extraction_task_id": feature_extraction_task_id,
        "current": current_step,
        "total": steps,
        "completed": current_step - 1,
        "status_message": status_message,
    }

    # Update task state in Celery
    update_task_state(task, status, meta)

    # Send Socket.IO message
    socketio_body = get_socketio_body_feature_task(
        task.request.id,
        feature_extraction_task_id,
        status,
        task_status_message(current_step, steps, status_message),
    )

    # Send Socket.IO message to clients about task
    socketio.emit(MessageType.FEATURE_TASK_STATUS.value, socketio_body)

    # Send Socket.IO message to clients about extraction
    send_extraction_status_message(feature_extraction_id, celery, socketio)


def update_task_state(task, state, meta):
    task.update_state(state=state, meta=meta)


def get_dicom_url_list(token, study_uid):
    # Get the metadata items of the study
    study_metadata_url = (
        endpoints.studies + "/" + study_uid + "/" + endpoints.studyMetadataSuffix
    )

    access_token = get_token_header(token)

    study_metadata = requests.get(study_metadata_url, headers=access_token).json()

    # instance_urls = {"CT": [], "PET": []}
    instance_urls = []

    # Go through each study metadata item
    for entry in study_metadata:

        # Determine the modality
        modality = None
        if entry[dicomFields.MODALITY][dicomFields.VALUE][0] == "PT":
            modality = "PET"
        elif entry[dicomFields.MODALITY][dicomFields.VALUE][0] == "CT":
            modality = "CT"
        elif entry[dicomFields.MODALITY][dicomFields.VALUE][0] == "MR":
            modality = "MRI"
        elif entry[dicomFields.MODALITY][dicomFields.VALUE][0] == "RTSTRUCT":
            modality = "RTSTRUCT"

        # If PET/CT, add URL to corresponding list
        series_uid = entry[dicomFields.SERIES_UID][dicomFields.VALUE][0]
        instance_uid = entry[dicomFields.INSTANCE_UID][dicomFields.VALUE][0]
        final_url = (
            endpoints.studies
            + "/"
            + study_uid
            + endpoints.seriesSuffix
            + "/"
            + series_uid
            + endpoints.instancesSuffix
            + "/"
            + instance_uid
        )
        if modality:
            instance_urls.append(final_url)

    return instance_urls


def download_dicom_urls(token, urls):
    tmp_dir = tempfile.mkdtemp()

    access_token = get_token_header(token)
    for url in urls:
        filename = os.path.basename(url)

        import logging

        logging.getLogger("urllib3").setLevel(logging.ERROR)
        response = requests.get(url, headers=access_token)

        try:
            multipart_data = decoder.MultipartDecoder.from_response(response)
        except NonMultipartContentTypeException as e:
            print("Got a weird thing from Kheops, response was")
            print(response.text)
            raise e

        # Get first part, with the content
        part = multipart_data.parts[0]

        fh = open(os.path.join(tmp_dir, filename), "wb")
        fh.write(part.content)
        fh.close()

    return tmp_dir


def extract_all_features(
    task,
    dicom_dir,
    config_path,
    feature_extraction_id,
    feature_extraction_task_id=None,
    current_step=None,
    steps=None,
):
    config = json.loads(Path(config_path).read_text()) if config_path else None

    features_dict = {}

    # Status update - CONVERT
    current_step += 1
    status_message = "Pre-processing data"
    update_progress(
        task,
        feature_extraction_id,
        feature_extraction_task_id,
        current_step,
        steps,
        status_message,
    )

    # Pre-process the data ONCE for all backends
    backend_input = FeatureBackend.pre_process_data(dicom_dir)

    # Status update - EXTRACT
    current_step += 1
    status_message = "Extracting features"
    update_progress(
        task,
        feature_extraction_id,
        feature_extraction_task_id,
        current_step,
        steps,
        status_message,
    )

    for backend in config["backends"]:

        # only if some features were selected for this backend
        if len(config["backends"][backend]["features"]) > 0:
            # get extractor from feature backends
            extractor = feature_backends_map[backend](config["backends"][backend])
            features = extractor.extract_features(backend_input)
            features_dict.update(features)

            print("CONFIG!!!!")
            print(config["backends"][backend])

            print("BACKEND INPUT!!! ")
            print(json.dumps(backend_input))

            print("FEATURES!!!")
            print(features)

    result = features_dict

    return result
