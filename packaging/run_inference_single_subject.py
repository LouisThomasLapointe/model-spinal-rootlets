"""
This script is used to run inference on a single subject using a nnUNetV2 model.

Note: conda environment with nnUNetV2 is required to run this script.
For details how to install nnUNetV2, see:
https://github.com/ivadomed/utilities/blob/main/quick_start_guides/nnU-Net_quick_start_guide.md#installation

Author: Jan Valosek

Example:
    python run_inference_single_subject.py
        -i sub-001_T2w.nii.gz
        -o sub-001_T2w_label-rootlet.nii.gz
        -path-model <PATH_TO_MODEL_FOLDER>
"""


import os
import shutil
import subprocess
import argparse
import datetime

import torch
import glob
import time
import tempfile

from nnunetv2.inference.predict_from_raw_data import predict_from_raw_data as predictor


def get_parser():
    # parse command line arguments
    parser = argparse.ArgumentParser(description='Segment an image using nnUNet model.')
    parser.add_argument('-i', help='Input image to segment. Example: sub-001_T2w.nii.gz', required=True)
    parser.add_argument('-o', help='Output filename. Example: sub-001_T2w_label-rootlet.nii.gz', required=True)
    parser.add_argument('-path-model', help='Path to the model folder. This folder should contain individual '
                                            'folders like fold_0, fold_1, etc. and dataset.json, '
                                            'dataset_fingerprint.json and plans.json files.', required=True, type=str)
    parser.add_argument('-use-gpu', action='store_true', default=False,
                        help='Use GPU for inference. Default: False')
    parser.add_argument('-use-best-checkpoint', action='store_true', default=False,
                        help='Use the best checkpoint (instead of the final checkpoint) for prediction. '
                             'NOTE: nnUNet by default uses the final checkpoint. Default: False')

    return parser


def get_orientation(file):
    """
    Get the original orientation of an image
    :param file: path to the image
    :return: orig_orientation: original orientation of the image, e.g. LPI
    """

    # Fetch the original orientation from the output of sct_image
    sct_command = "sct_image -i {} -header | grep -E qform_[xyz] | awk '{{printf \"%s\", substr($2, 1, 1)}}'".format(
        file)
    orig_orientation = subprocess.check_output(sct_command, shell=True).decode('utf-8')
    print("original:",orig_orientation)
    return orig_orientation


def tmp_create():
    """
    Create temporary folder and return its path
    """
    prefix = f"sciseg_prediction_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_"
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    print(f"Creating temporary folder ({tmpdir})")
    return tmpdir


def splitext(fname):
    """
    Split a fname (folder/file + ext) into a folder/file and extension.
    Note: for .nii.gz the extension is understandably .nii.gz, not .gz
    (``os.path.splitext()`` would want to do the latter, hence the special case).
    Taken (shamelessly) from: https://github.com/spinalcordtoolbox/manual-correction/blob/main/utils.py
    """
    dir, filename = os.path.split(fname)
    for special_ext in ['.nii.gz', '.tar.gz']:
        if filename.endswith(special_ext):
            stem, ext = filename[:-len(special_ext)], special_ext
            return os.path.join(dir, stem), ext
    # If no special case, behaves like the regular splitext
    stem, ext = os.path.splitext(filename)
    return os.path.join(dir, stem), ext


def add_suffix(fname, suffix):
    """
    Add suffix between end of file name and extension. Taken (shamelessly) from:
    https://github.com/spinalcordtoolbox/manual-correction/blob/main/utils.py
    :param fname: absolute or relative file name. Example: t2.nii.gz
    :param suffix: suffix. Example: _mean
    :return: file name with suffix. Example: t2_mean.nii
    Examples:
    - add_suffix(t2.nii, _mean) -> t2_mean.nii
    - add_suffix(t2.nii.gz, a) -> t2a.nii.gz
    """
    stem, ext = splitext(fname)
    return os.path.join(stem + suffix + ext)


def main():
    parser = get_parser()
    args = parser.parse_args()

    fname_file = args.i
    fname_file_out = args.o
    print(f'\nFound {fname_file} file.')

    # Create temporary directory in the temp to store the reoriented images
    tmpdir = tmp_create()
    # Copy the file to the temporary directory using shutil.copyfile
    fname_file_tmp = os.path.join(tmpdir, os.path.basename(fname_file))
    shutil.copyfile(fname_file, fname_file_tmp)
    print(f'Copied {fname_file} to {fname_file_tmp}')

    # Get the original orientation of the image, for example LPI
    orig_orientation = get_orientation(fname_file_tmp)

    # Reorient the image to LPI orientation if not already in LPI
    if orig_orientation != 'LPI':
        print(f'Reorienting to LPI orientation...')
        # reorient the image to LPI using SCT
        os.system('sct_image -i {} -setorient LPI -o {}'.format(fname_file_tmp, fname_file_tmp))

    # NOTE: for individual images, the _0000 suffix is not needed.
    # BUT, the images should be in a list of lists
    fname_file_tmp_list = [[fname_file_tmp]]

    # Use all the folds available in the model folder by default
    folds_avail = [int(f.split('_')[-1]) for f in os.listdir(args.path_model) if f.startswith('fold_')]
    print(f'Using folds {folds_avail}')

    # Create directory for nnUNet prediction
    tmpdir_nnunet = os.path.join(tmpdir, 'nnUNet_prediction')
    fname_prediction = os.path.join(tmpdir_nnunet, os.path.basename(add_suffix(fname_file_tmp, '_pred')))
    os.mkdir(tmpdir_nnunet)

    # Run nnUNet prediction
    print('Starting inference...')
    start = time.time()
    # directly call the predict function
    predictor(
        list_of_lists_or_source_folder=fname_file_tmp_list,
        output_folder=tmpdir_nnunet,
        model_training_output_dir=args.path_model,
        use_folds=folds_avail,
        tile_step_size=0.9,     # changing it from 0.5 to 0.9 makes inference faster
        use_gaussian=True,      # applies gaussian noise and gaussian blur
        use_mirroring=False,    # test time augmentation by mirroring on all axes
        perform_everything_on_gpu=True if args.use_gpu else False,
        device=torch.device('cuda', 0) if args.use_gpu else torch.device('cpu'),
        verbose=False,
        save_probabilities=False,
        overwrite=True,
        checkpoint_name='checkpoint_final.pth' if not args.use_best_checkpoint else 'checkpoint_best.pth',
        num_processes_preprocessing=3,
        num_processes_segmentation_export=3
    )
    end = time.time()

    print('Inference done.')
    total_time = end - start
    print('Total inference time: {} minute(s) {} seconds'.format(int(total_time // 60), int(round(total_time % 60))))

    # Copy .nii.gz file from tmpdir_nnunet to tmpdir
    pred_file = glob.glob(os.path.join(tmpdir_nnunet, '*.nii.gz'))[0]
    shutil.copyfile(pred_file, fname_prediction)

    print('Re-orienting the prediction back to original orientation...')
    # Reorient the image back to original orientation
    # skip if already in LPI
    #if orig_orientation != 'LPI':
        #print(f'Reorienting to original orientation {orig_orientation}...')
        # reorient the image to the original orientation using SCT
        #os.system('sct_image -i {} -setorient {} -o {}'.format(fname_prediction, orig_orientation, fname_prediction))

    # Create binary segmentation
    os.system('sct_maths -i {} -bin 0 -o {}'.format(fname_prediction, add_suffix(fname_file_out, '_bin')))
    # Create level-specific (i.e., non-binary) segmentation
    shutil.copyfile(fname_prediction, fname_file_out)

    print('Deleting the temporary folder...')
    # Delete the temporary folder
    shutil.rmtree(tmpdir)

    print('-' * 50)
    print(f"Created {add_suffix(fname_file_out, '_bin')}.nii.gz")
    print(f"Created {fname_file_out}")
    print('-' * 50)


if __name__ == '__main__':
    main()
