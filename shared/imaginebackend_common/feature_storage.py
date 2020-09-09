from imaginebackend_common.models import Modality, ROI, FeatureDefinition, FeatureValue
from more_itertools import first_true


def store_features(feature_extraction_task_id, feature_family_id, features):

    # Store or retrieve metadata (modalities, ROIs & feature definitions) first
    modalities = list(features.keys())
    modality_instances = store_modalities(modalities)

    first_modality = next(iter(features.values()))
    rois = list(first_modality.keys())
    roi_instances = store_rois(rois)

    first_roi = next(iter(first_modality.values()))

    filtered_features = filter_out_diagnostics(first_roi)
    feature_names = list(filtered_features.keys())
    feature_definition_instances = store_feature_definitions(
        feature_names, feature_family_id
    )

    # Store feature values

    feature_value_instances = []

    # For each modality
    for modality, rois in features.items():

        modality_instance = first_true(
            modality_instances, None, lambda m: m.name == modality
        )
        # For each ROI
        for roi, roi_features in features[modality].items():

            roi_instance = first_true(roi_instances, None, lambda r: r.name == roi)

            filtered_features = filter_out_diagnostics(roi_features)

            # Store each feature (not diagnostics)
            for feature_name, feature_value in filtered_features.items():

                feature_definition_instance = first_true(
                    feature_definition_instances,
                    None,
                    lambda fd: fd.name == feature_name
                    and fd.feature_family_id == feature_family_id,
                )

                feature_value_instance = FeatureValue(
                    feature_value,
                    feature_definition_instance.id,
                    feature_extraction_task_id,
                    modality_instance.id,
                    roi_instance.id,
                )
                feature_value_instance.save_to_db()

                feature_value_instances.append(feature_value_instance)

    print(feature_value_instances)
    return feature_value_instances


def store_modalities(modalities):
    modality_instances = []

    for modality in modalities:
        instance, created = Modality.get_or_create(
            criteria={"name": modality,}, defaults={"name": modality}
        )
        modality_instances.append(instance)

    return modality_instances


def store_rois(rois):
    roi_instances = []

    for roi in rois:
        instance, created = ROI.get_or_create(
            criteria={"name": roi,}, defaults={"name": roi}
        )
        roi_instances.append(instance)

    return roi_instances


def store_feature_definitions(feature_names, feature_family_id):
    feature_definition_instances = []

    for feature_name in feature_names:
        instance, created = FeatureDefinition.get_or_create(
            criteria={"name": feature_name, "feature_family_id": feature_family_id},
            defaults={"name": feature_name, "feature_family_id": feature_family_id},
        )
        feature_definition_instances.append(instance)

    return feature_definition_instances


def filter_out_diagnostics(features_dict):
    leave_out_prefix = "diagnostics"

    filtered_features_dict = {
        k: v for (k, v) in features_dict.items() if not k.startswith(leave_out_prefix)
    }

    return filtered_features_dict
