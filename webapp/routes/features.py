from flask import Blueprint, jsonify, request, g, current_app, Response

import numpy

from random import randint

from keycloak.realm import KeycloakRealm

from config import oidc_client
from imaginebackend_common.utils import (
    fetch_extraction_result,
    format_extraction,
    read_feature_file,
)
from service.feature_analysis import train_model_with_metric
from service.feature_extraction import (
    run_feature_extraction,
    get_studies_from_album,
    get_album_details,
)

from imaginebackend_common.models import FeatureExtraction, FeatureExtractionTask
from service.feature_transformation import (
    transform_study_features_to_tabular,
    make_study_file_name,
    transform_studies_features_to_csv,
    get_csv_file_content,
    make_album_file_name,
    separate_features_by_modality_and_roi,
)

from .utils import validate_decorate

from zipfile import ZipFile, ZIP_DEFLATED

import csv
import io
import os
import pandas


# Define blueprint
bp = Blueprint(__name__, "features")

# Constants
DATE_FORMAT = "%d.%m.%Y %H:%M"


@bp.before_request
def before_request():
    if not request.path.endswith("download"):
        validate_decorate(request)


@bp.route("/")
def hello():
    return "Hello IMAGINE!"


# Extraction of a study
@bp.route("/extractions/study/<study_uid>")
def extraction_by_study(study_uid):
    user_id = g.user

    # Find the latest task linked to this study
    latest_task_of_study = FeatureExtractionTask.find_latest_by_user_and_study(
        user_id, study_uid
    )

    # Use the latest feature extraction for this study OR an album that includes this study
    if latest_task_of_study:
        latest_extraction_of_study = latest_task_of_study.feature_extraction
        return jsonify(format_extraction(latest_extraction_of_study))
    else:
        return jsonify(None)


# Get feature payload for a given feature extraction
@bp.route("/extractions/<id>")
def extraction_by_id(id):
    feature_extraction = FeatureExtraction.find_by_id(id)

    return jsonify(format_extraction(feature_extraction, payload=True))


# Get feature details for a given extraction
@bp.route("/extractions/<id>/feature-details")
def extraction_features_by_id(id):
    # TODO - Add support for making this work for a single study as well
    token = g.token

    extraction = FeatureExtraction.find_by_id(id)
    studies = get_studies_from_album(extraction.album_id, token)

    [header, features] = transform_studies_features_to_csv(extraction, studies)

    return jsonify({"header": header, "features": features})


# Download features in CSV format
@bp.route("/extractions/<id>/download")  # ?patientID=???&studyDate=??? OR ?userID=???
def download_extraction_by_id(id):

    # Get the feature extraction to process from the DB
    feature_extraction = FeatureExtraction.find_by_id(id)

    # Get the names of the used feature families (for the file name so far)
    feature_families = []
    for extraction_family in feature_extraction.families:
        feature_families.append(extraction_family.feature_family.name)

    csv_header = None
    csv_data = None
    csv_file_content = None
    album_name = None

    user_id = request.args.get("userID", None)
    if user_id:  # Download for an album
        # Get a token for the given user (possible thanks to token exchange in Keycloak)
        token = oidc_client.token_exchange(
            requested_token_type="urn:ietf:params:oauth:token-type:access_token",
            audience=os.environ["KEYCLOAK_IMAGINE_CLIENT_ID"],
            requested_subject=user_id,
        )["access_token"]

        album_name = get_album_details(feature_extraction.album_id, token)["name"]
        album_studies = get_studies_from_album(feature_extraction.album_id, token)

        [csv_header, csv_data] = transform_studies_features_to_csv(
            feature_extraction, album_studies
        )
    else:  # Download for a single study
        patient_id = request.args.get("patientID", None)
        study_uid = request.args.get("studyUID", None)
        study_date = request.args.get("studyDate", None)

        # Make sure to only keep tasks related to the study
        # A Feature Extraction MAY contain tasks unrelated
        # to the current study if it was an album extraction
        study_tasks = list(
            filter(lambda task: task.study_uid == study_uid, feature_extraction.tasks)
        )

        [csv_header, csv_data] = transform_study_features_to_tabular(
            study_tasks, patient_id
        )

    csv_data_with_header = [csv_header] + csv_data
    csv_file_content = get_csv_file_content(csv_data_with_header)

    if album_name:
        # Album : send back a zip file with CSV files separated by
        # - Modality : PT/CT features shouldn't be mixed for example
        # - ROI : Main tumor & metastases features shouldn't be mixed for example
        grouped_features = separate_features_by_modality_and_roi(csv_file_content)

        # Create ZIP file to return
        zip_buffer = io.BytesIO()
        with ZipFile(zip_buffer, "a", ZIP_DEFLATED, False) as zip_file:
            for group_name, group_data in grouped_features.items():
                group_string_mem = io.StringIO()
                csv_writer = csv.writer(group_string_mem)
                csv_writer.writerows([csv_header] + group_data)

                group_file_name = f"features_album_{album_name}_{'-'.join(feature_families)}_{group_name}.csv"
                zip_file.writestr(group_file_name, group_string_mem.getvalue())

        file_name = make_album_file_name(album_name, feature_families)

        return Response(
            zip_buffer.getvalue(),
            mimetype="application/zip",
            headers={"Content-disposition": f"attachment; filename={file_name}"},
        )
    else:
        # Single study : just send back a CSV file with the various columns
        file_name = make_study_file_name(patient_id, study_date, feature_families)

        return Response(
            csv_file_content,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename={file_name}"},
        )


# Extractions by album
@bp.route("/extractions/album/<album_id>")
def extractions_by_album(album_id):
    user_id = g.user

    # Find latest feature extraction for this album
    latest_extraction_of_album = FeatureExtraction.find_latest_by_user_and_album_id(
        user_id, album_id
    )

    if latest_extraction_of_album:
        return jsonify(format_extraction(latest_extraction_of_album))
    else:
        return jsonify(None)


# Status of a feature extraction
@bp.route("/extractions/<extraction_id>/status")
def extraction_status(extraction_id):
    extraction = FeatureExtraction.find_by_id(extraction_id)

    status = fetch_extraction_result(current_app.my_celery, extraction.result_id)

    response = vars(status)

    return jsonify(response)


# Feature extraction for a study
@bp.route("/extract/study/<study_uid>", methods=["POST"])
def extract_study(study_uid):
    user_id = g.user

    feature_families_map = request.json

    # Define feature families to extract
    feature_extraction = run_feature_extraction(
        user_id, None, feature_families_map, study_uid
    )

    return jsonify(format_extraction(feature_extraction))


# Feature extraction for an album
@bp.route("/extract/album/<album_id>", methods=["POST"])
def extract_album(album_id):
    user_id = g.user
    token = g.token

    feature_families_map = request.json

    # Define feature families to extract
    feature_extraction = run_feature_extraction(
        user_id, album_id, feature_families_map, None, token
    )

    return jsonify(format_extraction(feature_extraction))
