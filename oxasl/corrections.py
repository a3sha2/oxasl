#!/bin/env python
"""
Motion and distortion corrections for ASL

The functions in this module calculate linear or non-linear correction transformations
to apply to the ASL and calibration images. Once calculated the ``apply_corrections``
function generates corrected images with the minimum of interpolation.

Currently four sources of transformation exist:

 - Motion correction of the ASL data. This generates a series of linear (rigid body)
   transformations in ASL space, one for each ASL volume. If calibration data is also 
   present a calibration->ASL transform is also generated as part of this process

 - Fieldmap-based distortion correction. This generates a nonlinear warp image
   in structural space which is then transformed to ASL space

 - Phase encoding reversed (CBLIP) distortion correction using TOPUP. This generates
   a nonlinear warp image in ASL space FIXME calibration space?

 - User-supplied nonlinear warp image for gradient distortion corection

Except for the TOPUP correction, all of the above can be combined in a single
transformation to minimise interpolation of the ASL data

Copyright (c) 2008-2013 Univerisity of Oxford
"""

import numpy as np

import fsl.wrappers as fsl
from fsl.data.image import Image

from .options import OptionCategory, IgnorableOptionGroup
from .reporting import ReportPage
from .wrappers import epi_reg
import oxasl.struc as struc
import oxasl.reg as reg

class DistcorrOptions(OptionCategory):
    """
    OptionCategory which contains options for distortion correction
    """

    def __init__(self, **kwargs):
        OptionCategory.__init__(self, "distcorr", **kwargs)

    def groups(self, parser):
        ret = []
        g = IgnorableOptionGroup(parser, "Distortion correction using fieldmap")
        g.add_option("--fmap", help="fieldmap image (in rad/s)", type="image")
        g.add_option("--fmapmag", help="fieldmap magnitude image - wholehead extracted", type="image")
        g.add_option("--fmapmagbrain", help="fieldmap magnitude image - brain extracted", type="image")
        g.add_option("--nofmapreg", help="Do not perform registration of fmap to T1 (use if fmap already in T1-space)", action="store_true", default=False)
        ret.append(g)

        g = IgnorableOptionGroup(parser, "Distortion correction using phase-encode-reversed calibration image (TOPUP)")
        g.add_option("--cblip", help="phase-encode-reversed (blipped) calibration image", type="image")
        ret.append(g)

        g = IgnorableOptionGroup(parser, "General distortion correction options")
        g.add_option("--echospacing", help="Effective EPI echo spacing (sometimes called dwell time) - in seconds", type=float)
        g.add_option("--pedir", help="Phase encoding direction, dir = x/y/z/-x/-y/-z")
        g.add_option("--gdcwarp", help="Additional warp image for gradient distortion correction - will be combined with fieldmap or TOPUP distortion correction", type="image")
        ret.append(g)

        g = IgnorableOptionGroup(parser, "Sensitivity correction")
        g.add_option("--isen", help="User-supplied sensitivity correction in ASL space")
        g.add_option("--senscorr-auto", help="Apply automatic sensitivity correction using bias field from FAST", action="store_true", default=False)
        g.add_option("--senscorr-off", help="Do not apply any sensitivity correction", action="store_true", default=False)
        ret.append(g)

        g = IgnorableOptionGroup(parser, "Partial volume correction")
        g.add_option("--pvcorr", help="Apply partial volume correction", action="store_true", default=False)
        ret.append(g)

        return ret

def get_cblip_correction(wsp):
    """
    Get the cblip based distortion correction warp

    Required workspace attributes
    -----------------------------

     - ``calib`` : Calibration image
     - ``cblip`` : Phase-encode-reversed calibration image
     - ``echospacing`` :
     - ``pedir`` : 

    Optional workspace attributes
    -----------------------------

    Updated workspace attributes
    ----------------------------

     - ``cblip_warp``    : CBLIP Distortion correction warp image
     
    """
    if wsp.isdone("get_cblip_correction"):
        return

    wsp.log.write("Distortion Correction using TOPUP\n")

    topup_params = {
        "x"  : " 1  0  0 %f\n-1  0  0 %f",
        "-x" : "-1  0  0 %f\n 1  0  0 %f",
        "y"  : " 0  1  0 %f\n 0 -1  0 %f",
        "-y" : " 0 -1  0 %f\n 0  1  0 %f",
        "z"  : " 0  0  1 %f\n 0  0 -1 %f",
        "-z" : " 0  0 -1 %f\n 0  0  1 %f",
    }
    params = topup_params[wsp.pedir] % (wsp.echospacing, wsp.echospacing)
    
    # do topup
    wsp.calib_blipped = Image(np.stack((wsp.calib.data, wsp.cblip.data), axis=-1), header=wsp.calib.header)
    topup_result = fsl.topup(imain=wsp.calib_blipped, datain=params, config="b02b0.cnf", out=fsl.LOAD, fout=fsl.LOAD)
    wsp.done("get_cblip_correction")

def get_fieldmap_correction(wsp):
    """
    Get the fieldmap based distortion correction warp

    Required workspace attributes
    -----------------------------

     - ``pwi``          : Perfusion weighted image (generated by preproc_asl)
     - ``fmap``         : Fieldmap image
     - ``fmapmag``      : Fieldmap magnitude image
     - ``fmapmagbrain`` : Fieldmap magnitude brain image
     - ``echospacing``  :
     - ``pedir``        :

    Optional workspace attributes
    -----------------------------

     - ``nofmapreg``       : If True assume fieldmap in structural space

    Updated workspace attributes
    ----------------------------

     - ``fmap_warp``    : Fieldmap distortion correction warp image in ASL space
     
    """
    if wsp.isdone("get_fieldmap_correction"):
        return

    if wsp.fmap is None or wsp.fmapmag is None or wsp.fmapmagbrain is None:
        wsp.log.write("\nNo fieldmap images for distortion correction\n")
    elif wsp.pedir is None or wsp.echospacing is None:
        wsp.log.write("\nWARNING: Fieldmap images supplied but pedir and echospacing required for distortion correction\n")
    else:
        reg.reg_asl2struc(wsp)
        struc.segment(wsp)

        wsp.log.write("\nDistortion correction from fieldmap images using EPI_REG\n")

        epi_reg_opts = {
            "inweight" : wsp.inweight,
            "init" : wsp.asl2struc,
            "fmap" : wsp.fmap,
            "fmapmag" : wsp.fmapmag,
            "fmapmagbrain" : wsp.fmapmagbrain,
            "pedir" : wsp.pedir,
            "echospacing" : wsp.echospacing,
            "nofmapreg" : wsp.ifnone("nofmapreg", False),
        }
        
        result = epi_reg(epi=wsp.pwi, t1=wsp.struc, t1brain=wsp.struc_brain, out=fsl.LOAD, wmseg=wsp.wm_seg_struc, **epi_reg_opts)
        wsp.fmap_warp_struc = result["out_warp"]
        wsp.fmap_asl2struc = result["out"]
        wsp.fmap_struc2asl = np.linalg.inv(wsp.fmap_asl2struc)

        result = fsl.convertwarp(out=fsl.LOAD, ref=wsp.asldata_mean, warp1=wsp.fmap_warp_struc, postmat=wsp.fmap_struc2asl, rel=True)
        wsp.fmap_warp = result["out"]
        
    wsp.done("get_fieldmap_correction")

def get_motion_correction(wsp):
    """
    Calculate motion correction transforms for ASL data
    
    Note simple motion correction of multi-volume calibration data is done in preprocessing.

    The reference volume for motion correction is the calibration image, if supplied, or
    otherwise the middle volume of the ASL data is used. 
    
    If the calibration image is used, the inverse of the middle ASL volume -> calibration
    transform is applied to each transform matrix. This ensures that the middle volume of 
    the ASL data is unchanged and interpolation on the other volumes is also minimised.
    In this case, all calibration images are also themselves transformed to bring them in
    to ASL middle volume space.

    Required workspace attributes
    -----------------------------

     - ``asldata`` : ASL data image

    Optional workspace attributes
    -----------------------------

     - ``calib``    : Calibration image

    Updated workspace attributes
    ----------------------------

     - ``asldata_mc_mats`` : Sequence of matrices giving motion correction transform for each ASL volume
     - ``asl2calib``       : ASL->calibration image transformation
     - ``calib2asl``       : Calibration->ASL image transformation
    """
    if wsp.isdone("get_motion_correction"):
        return

    wsp.log.write("\nMotion Correction\n")
    # If available, use the calibration image as reference since this will be most consistent if the data has a range 
    # of different TIs and background suppression etc. This also removes motion effects between asldata and calibration image
    if wsp.calib:
        wsp.log.write(" - Using calibration image as reference\n")
        ref_source = "calibration image: %s" % wsp.calib.name
        mcflirt_result = fsl.mcflirt(wsp.asldata, reffile=wsp.calib, out=fsl.LOAD, mats=fsl.LOAD, log=wsp.fsllog)
        mats = [mcflirt_result["out.mat/MAT_%04i" % vol] for vol in range(wsp.asldata.shape[3])]

        # To reduce interpolation of the ASL data change the transformations so that we end up in the space of the central volume of asldata
        wsp.asl2calib = mats[int(float(len(mats))/2)]
        wsp.calib2asl = np.linalg.inv(wsp.asl2calib)
        mats = [np.dot(mat, wsp.calib2asl) for mat in mats]
        wsp.log.write("   ASL middle volume->Calib:\n%s\n" % str(wsp.asl2calib))
        wsp.log.write("   Calib->ASL middle volume:\n%s\n" % str(wsp.calib2asl))
    else:
        wsp.log.write(" - Using ASL data middle volume as reference\n")
        ref_source = "ASL data %s middle volume: %i" % (wsp.asldata.name, int(float(wsp.asldata.shape[3])/2))
        mcflirt_result = fsl.mcflirt(wsp.asldata, out=fsl.LOAD, mats=fsl.LOAD, log=wsp.fsllog)
        mats = [mcflirt_result["out.mat/MAT_%04i" % vol] for vol in range(wsp.asldata.shape[3])]
        
    # Convert motion correction matrices into single (4*nvols, 4) matrix - convenient for writing
    # to file, and same form that applywarp expects
    wsp.asldata_mc_mats = np.concatenate(mats, axis=0)

    page = ReportPage()
    page.heading("Motion correction", level=0)
    page.text("Reference volume: %s" % ref_source)
    page.heading("Motion parameters", level=1)
    for vol, mat in enumerate(mats):
        page.text("Volume %i" % vol)
        page.matrix(mat)
    wsp.report.add("moco", page)

    wsp.done("get_motion_correction")

def get_sensitivity_correction(wsp):
    """
    Get sensitivity correction image
    
    Required workspace attributes
    -----------------------------

     - ``asldata`` : ASL data

    Optional workspace attributes
    -----------------------------

     - ``isen`` : User supplied sensitivity image
     - ``calib`` : Calibration image. Used in conjunction with ``cref`` to calculate sensitivity map
     - ``cref`` : Calibration reference image 
     - ``senscorr_auto`` : If True, automatically calculate sensitivity correction using FAST
     - ``senscorr_off`` If True, do not apply sensitivity correction

    Updated workspace attributes
    ----------------------------

     - ``sensitivity``    : Sensitivity correction image in ASL space
    """
    if wsp.sensitivity is None:
        wsp.log.write("Sensitivity correction\n")
        if wsp.senscorr_off:
            wsp.log.write(" - Sensitivity correction disabled\n")
        elif wsp.isen is not None:
            wsp.log.write(" - Sensitivity image supplied by user\n")
        elif wsp.calib is not None and wsp.cref is not None:
            wsp.log.write(" - Sensitivity image calculated from calibration reference image\n")
            wsp.sensitivity = Image(wsp.calib.data / wsp.cref.data, header=wsp.calib.header)
        elif wsp.senscorr_auto and wsp.bias is not None:
            struc.segment(wsp)
            wsp.log.write(" - Sensitivity image calculated from bias field\n")
            sens = Image(np.reciprocal(wsp.bias.data), header=wsp.bias.header)           
            reg.reg_asl2struc(wsp)
            wsp.sensitivity = fsl.applyxfm(sens, wsp.regfrom, wsp.struc2asl, out=fsl.LOAD, interp="trilinear", log=wsp.fsllog)["out"]
        else:
            wsp.log.write(" - No source of sensitivity correction was found\n")
        wsp.log.write("\n")

def apply_corrections(wsp):
    """
    Apply distortion and motion corrections to ASL and calibration data

    Required workspace attributes
    -----------------------------

     - ``asldata_orig`` : Uncorrected ASL data image

    Optional workspace attributes
    -----------------------------

     - ``calib_orig``      : Calibration image
     - ``cref_orig``       : Calibration reference image
     - ``cblip_orig``      : Calibration BLIP image
     - ``asldata_mc_mats`` : ASL motion correction matrices
     - ``calib2asl``       : Calibration -> ASL transformation matrix
     - ``distcorr_warp``   : Distortion correction warp image
     - ``gdc_warp``        : Gradient distortion correction warp image

    Updated workspace attributes
    ----------------------------

     - ``asldata``    : Corrected ASL data
     - ``calib``      : Corrected calibration image
     - ``cref``       : Corrected calibration reference image
     - ``cblip``      : Corrected calibration BLIP image
    """
    wsp.log.write("\nApplying combined corrections to data\n")

    if wsp.asldata_mc_mats is not None:
        wsp.log.write(" - Adding motion correction transforms\n")

    warps = []
    if wsp.fmap_warp:
        wsp.log.write(" - Adding fieldmap based warp to correction\n")
        warps.append(wsp.fmap_warp)
    
    if wsp.gdc_warp:
        wsp.log.write(" - Adding user-supplied GDC warp to correction\n")
        warps.append(wsp.gdc_warp)
        
    if warps:
        kwargs = {}
        for idx, warp in enumerate(warps):
            kwargs["warp%i" % (idx+1)] = warp
                
        wsp.log.write(" - Converting all warps to single transform and extracting Jacobian\n")
        fsl.convertwarp(ref=wsp.asldata_mean, out=fsl.LOAD, rel=True, jacobian=fsl.LOAD, **kwargs)
        # FIXME save jacobian

    if not warps and wsp.asldata_mc_mats is None:
        wsp.log.write(" - No corrections to apply\n")
        return

    # Apply all corrections to ASL data - note that we make sure the output keeps all the ASL metadata
    wsp.log.write(" - Applying corrections to ASL data\n")
    asldata_img = correct_img(wsp, wsp.asldata_orig, wsp.asldata_mc_mats)
    wsp.asldata = wsp.asldata_orig.derived(asldata_img.data)

    # Apply corrections to calibration images
    if wsp.calib_orig is not None:
        wsp.log.write(" - Applying corrections to calibration data\n")
        wsp.calib = correct_img(wsp, wsp.calib_orig, wsp.calib2asl)
    
        if wsp.cref_orig is not None:
            wsp.cref = correct_img(wsp, wsp.cref_orig, wsp.calib2asl)
        if wsp.cblip_orig is not None:
            wsp.cblip = correct_img(wsp, wsp.cref_cblip, wsp.calib2asl)

    # FIXME now need to apply TOPUP correction?

def correct_img(wsp, img, linear_mat):
    """
    Apply combined warp/linear transformations to an image
    
    :param img: fsl.data.image.Image to correct
    :param linear_mat: img->ASL space linear transformation matrix.
    :return: Corrected Image

    If a jacobian is present, also corrects for quantitative signal magnitude as volume has been locally scaled

    FIXME there are slight differences to oxford_asl here due to use of spline interpolation rather than
    applyxfm4D which uses sinc interpolation.

    Required workspace attributes
    -----------------------------

     - ``asldata_mean`` : Mean ASL image used as reference space

    Optional workspace attributes
    -----------------------------

     - ``total_warp``      : Combined warp image
     - ``jacobian``        : Jacobian associated with warp image
    """
    warp_result = fsl.applywarp(img, wsp.asldata_mean, out=fsl.LOAD, warp=wsp.total_warp, premat=linear_mat, interp="sinc", paddingsize=1, rel=True)
    img = warp_result["out"]
    if wsp.jacobian:
        wsp.log.write(" - Correcting for local volume scaling using Jacobian\n")
        img = Image(img.data * wsp.jacobian)
    return img

def apply_sensitivity_correction(wsp, *imgs):
    """
    Apply sensitivity correction

    :param imgs: Sequence of Image objects

    :return: Tuple of corrected Image objects corresponding to input.
             If no sensitivity correction is defined, returns the same
             images as input.

    Optional workspace attributes
    -----------------------------

     - ``sensitivity``  : Sensitivity correction image
     - ``senscorr_off`` : If True, no correction will be applied even if ``sensitivity`` image exists
    """
    if wsp.sensitivity is not None and not wsp.senscorr_off:
        wsp.log.write("Applying sensitivity correction\n")
        ret = []
        for img in imgs:
            corrected = img.data / wsp.sensitivity.data
            ret.append(Image(corrected, header=img.header))
        return tuple(ret)
    else:
        return tuple(imgs)
