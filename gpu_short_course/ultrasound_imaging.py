import numpy as np
import cupy as cp
from numba import cuda, float32
import numba
import math
import scipy.signal as signal
import matplotlib.pyplot as plt
from scipy.io import loadmat
import pickle
from cupyx.scipy import fftpack
import cupyx.scipy.ndimage
from matplotlib import animation





def create_grid(x_mm, z_mm, nx=128, nz=128):
    xgrid = np.linspace(x_mm[0] * 1e-3, x_mm[1] * 1e-3, nx)
    zgrid = np.linspace(z_mm[0] * 1e-3, z_mm[1] * 1e-3, nz)
    return xgrid, zgrid


# DEFAULT GRID
X_MM = [-15, 15]
Z_MM = [20, 50]
DEFAULT_X_GRID, DEFAULT_Z_GRID = create_grid(X_MM, Z_MM)


def read_data(filepath):
    data = pickle.load(open(filepath, 'rb'))
    rf = data["data"]
    context = data.copy()
    context.pop("data", None)
    return rf, context


# device function
@cuda.jit(device=True)
def calc_pix_val(rf, px, pz, angles, elx, c, fs):
    """
    Returns the value in single pixel of the reconstructed image
    for Plane Wave Imaging scheme with tx angle = 0 degree.

    :param rf: 2D array of ultrasound signals acquired by the transducer.
        rf.shape = (number_of_elements, number_of_samples)
    :param px: x coordinate of the image pixel
    :param pz: z coordinate of the image pixel
    :param angle: wave angle of incidence
    :param elx: vector of x coordinates of the transducer elements
    :param c: speed of sound in the medium
    :param fs: sampling frequency
    """
    n_samples = rf.shape[-1]
    # sum suitable samples from signals acquired by each transducer element
    value = float32(0.0)
    for transmit, angle in enumerate(angles):
        if angle >= 0:
            pw = elx[0]
        else:
            pw = elx[-1]
        for element in range(len(elx)):
            # Nearest-neighbour interpolation.
            sample_number = round((pz * math.cos(angle)
                                   - pw * math.sin(angle)
                                   + px * math.sin(angle)
                                   + math.sqrt(
                        pz ** 2 + (px - elx[element]) ** 2)) / c * fs)
            if sample_number < n_samples:
                value += rf[transmit, element, sample_number]
    return value


@cuda.jit
def reconstruct(image, rf, x_grid, z_grid, angle, elx, c, fs):
    """
    Enumerates values in all pixels of the reconstructed image
    for Plane Wave Imaging scheme.

    :param image: pre-allocated device ndarray for image data
    :param rf: ultrasound signals acquired by the transducer
    :param xgrid: 2D array of image pixels x coordinates
    :param zgrid: 2D array of image pixels z coordinates
    :param elx: vector of x coordinates of the transducer elements
    :param c: speed of sound in the medium
    :param fs: sampling frequency

    """
    z, x = cuda.grid(2)
    if x > image.shape[0] or z > image.shape[1]:
        return
    # Kernel does not return a value - it modifies one of arguments into result.
    image[x, z] = calc_pix_val(rf, x_grid[x], z_grid[z], angle, elx, c, fs)


def beamform(rf, context, stream):
    """
    Function beamforming ultrasound data into image using GPU kernel.

    :param rf: ultrasound signals acquired by the transducer
    :param xgrid: vector of image pixels x coordinates
    :param zgrid: vector of image pixels z coordinates
    :param context: a dictionary describing the acquistion context
    """
    # Wrap the CuPy stream object into a Numba stream object.
    stream = cuda.external_stream(stream.ptr)
    c = context["c"]
    fs = context["fs"]
    output = context["beamform_output"]
    x_grid = context["x_grid"]
    z_grid = context["z_grid"]
    angle = context["angle"]
    elx = context["elx"]

    nz = len(z_grid)
    nx = len(x_grid)

    block = (32, 32)
    gridshape_z = (nz + block[0] - 1) // block[0]
    gridshape_x = (nx + block[1] - 1) // block[1]
    grid = (gridshape_z, gridshape_x)
    reconstruct[grid, block, stream](output, rf, x_grid, z_grid, angle, elx, c,
                                     fs)
    return output


def init_beamformer(data, context, x_grid=None, z_grid=None):
    if x_grid is None:
        x_grid = DEFAULT_X_GRID
    if z_grid is None:
        z_grid = DEFAULT_Z_GRID

    n_transmissions, n_channels, n_samples = data.shape
    angle = context["angle"]
    result = cuda.device_array((len(x_grid), len(z_grid)), dtype=np.float32)
    x_grid = cuda.to_device(x_grid)
    z_grid = cuda.to_device(z_grid)
    angle = cuda.to_device(angle)  # TODO move to const memory

    probe_width = (n_channels - 1) * context["pitch"]
    elx = np.linspace(-probe_width / 2, probe_width / 2,
                      n_channels)  # TODO move to const memory
    elx = cuda.to_device(elx)
    update = {
        "beamform_output": result,
        "angle": angle,
        "x_grid": x_grid,
        "z_grid": z_grid,
        "elx": elx
    }
    return {
        **context,
        **update
    }


def _create_hilbert_coeffs(n):
    ndim = 2
    axis = -1
    h = cp.zeros(n).astype(cp.float32)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1
        h[1:n // 2] = 2
    else:
        h[0] = 1
        h[1:(n + 1) // 2] = 2
    indices = [cp.newaxis] * ndim
    indices[axis] = slice(None)
    h = h[tuple(indices)]
    return h


def hilbert(data, context, stream):
    h = context.get("hilbert_coeffs", None)
    if h is None or h.shape[-1] != data.shape[-1]:
        h = _create_hilbert_coeffs(data.shape[-1])
        context["hilbert_coeffs"] = h
    with stream:
        data = cp.asarray(data)
        xf = fftpack.fft(data, axis=-1)
        result = fftpack.ifft(xf*h, axis=-1)
        return cp.abs(result)


def to_bmode(envelope, stream):
    with stream:
        maximum = cp.max(envelope)
        envelope = envelope / maximum
        envelope = 20 * cp.log10(envelope)
        return envelope


def display_bmode(img, x=None, z=None):
    if x is None:
        x = X_MM
    if z is None:
        z = Z_MM
    fig, ax = plt.subplots()
    ax.set_xlabel('OX [mm]')
    ax.set_ylabel('OZ [mm]')
    ax.imshow(img, extent=x + z[::-1], cmap='gray', vmin=-30, vmax=0)


def dB(data, mx=1):
    return 20*np.log10(data/mx)


def iq2bmode(iq):
    env = np.abs(iq)
    mx = np.nanmax(env)
    env = dB(env, mx=mx)
    return env


def iq2bmode_gpu(iq, mx=1):
    env = cp.abs(iq)
    mx = cp.nanmax(env)
    env = 20*cp.log10(env/mx)
    return env

def power_mask(data, power_dB, power_limit):
    mask = (power_dB < power_limit[0]) | (power_dB >= power_limit[1])
    img = np.copy(data)
    img[mask] = None
    return img

def scale_doppler(doppler_data, doppler_type):

    if doppler_type == 'color':
        doppler_scaled = doppler_data

    elif doppler_type == 'doppler frequency':
        doppler_scaled = doppler_data*1e-3

    elif doppler_type == 'speed':
        doppler_scaled = doppler_data*1e3

    elif doppler_type == 'power':
        doppler_scaled = doppler_data

    elif doppler_type == 'noflow':
        doppler_scaled = doppler_data*np.nan

    else:
        raise ValueError(
        "The 'doppler_type' parameter should be one of the following: "
        "'color', 'power', 'speed', 'doppler frequency' or 'noflow'. ")
    return doppler_scaled

def prepare_doppler(doppler_array, power_dB, power_limit, doppler_type):
    doppler_array = scale_doppler(doppler_array, doppler_type)
    return  power_mask(doppler_array, power_dB, power_limit)





def show_flow(
    bmode, doppler_array, power_dB,
    xgrid=None,
    zgrid=None,
    doppler_type='power',
    power_limit=(26,56),
    color_limit=None,
    bmode_limit=(-60,0)):

    """
    The function show blood flow on the b-mode image.

    :param bmode: bmode data array,
    :param doppler_array: doppler data array (i.e. raw color,
                          doppler frequency or blood speed),
    :param power_dB: power data array (in [dB]),
    :param xgrid: (optional) vector of 'x' coordinates in [m],
    :param zgrid: (optional) vector of 'z' coordinates in [m],
    :param doppler_type:(optional) type of flow presentation,
        the following types are possible:
        1. 'color' - raw color estimate [radians],
        2. 'doppler frequency' - color scaled in [kHz],
        3. 'power' - power estimate in [dB],
        4. 'speed' - color scaled in [mm/s],
        5. 'noflow' - bmode only,
    :param power_limit: (optional) flow estimate pixels corresponding to
                            power outside power_limit will not be shown,
    :param color_limit: (optional) two element tuple with color limit,
    :param bmode_limit: (optional) two element tuple with bmode limit.

    """

    if doppler_type == 'power':
        doppler_array = power_dB

    if xgrid is not None and zgrid is not None:
        # convert grid from [m] to [mm]
        xgrid = xgrid*1e3
        zgrid = zgrid*1e3
        extent = (min(xgrid), max(xgrid), max(zgrid), min(zgrid))

        # calculate data aspect for proper image proportions
        dx = xgrid[1]-xgrid[0]
        dz = zgrid[1]-zgrid[0]
        data_aspect = dz/dx
        xlabel = '[mm]'
        ylabel = '[mm]'

    else:
        data_aspect = None
        extent = None
        xlabel = 'lines'
        ylabel = 'samples'


    # set appropriate parameters for plt.imshow()
    if doppler_type == 'color':
        cmap = 'bwr'
        title = 'color doppler'
        cbar_label = '[radians]'
        if color_limit is None:
            color_limit = (-1., 1.)

    elif doppler_type == 'doppler frequency':
        cmap = 'bwr'
        title = 'color doppler'
        cbar_label = '[kHz]'
        if color_limit is None:
            color_limit = (-1, 1)

    elif doppler_type == 'speed':
        cmap = 'bwr'
        title = 'color doppler'
        cbar_label = '[mm/s]'
        if color_limit is None:
            color_limit = (-40, 40)

    elif doppler_type == 'power':
        cmap = 'hot'
        title = 'power doppler'
        cbar_label = '[dB]'
        color_limit = None

    elif doppler_type == 'noflow':
        cmap = 'gray'
        title = 'B-mode'
        cbar_label = '[dB]'
        color_limit = bmode_limit

    else:
        raise ValueError(
            "The 'doppler_type' parameter should be one of the following: "
            "'color', 'power', 'speed', 'doppler frequency'. "
            )

    if color_limit is not None:
        vmin = color_limit[0]
        vmax = color_limit[1]

    else:
        vmin = None
        vmax = None

    # scale doppler data and mask pixels corresponding to too low or too high power (outside power_limit)
    doppler_data = prepare_doppler(doppler_array, power_dB, power_limit, doppler_type)

    # draw
    bmode_img = plt.imshow(
        bmode,
        interpolation='bicubic',
        aspect=data_aspect,
        cmap='gray',
        extent=extent,
        vmin=bmode_limit[0],
        vmax=bmode_limit[1],
    )

    flow_img = plt.imshow(
        doppler_data,
        interpolation='bicubic',
        aspect=data_aspect,
        cmap=cmap,
        extent=extent,
        vmin=vmin, vmax=vmax,
    )

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.tight_layout()
    plt.colorbar(
        flow_img,
        location='bottom',
        pad=0.2,
        aspect=20,
        fraction=0.05,
        label=cbar_label,
    )
    return bmode_img, flow_img, doppler_data


def show_flow_cineloop(
    bmode, doppler_array, power_dB,
    xgrid=None,
    zgrid=None,
    doppler_type='power',
    power_limit=(26,70),
    color_limit=None,
    bmode_limit=(-60,0)):

    """
    The function show animation of blood flow on the b-mode image.

    :param bmode: bmode data array,
    :param doppler_array: doppler data array (i.e. raw color, doppler frequency or blood speed),
    :param power_dB: power data array (in [dB]),
    :param xgrid: (optional) vector of 'x' coordinates in [m],
    :param zgrid: (optional) vector of 'z' coordinates in [m],
    :param doppler_type:(optional) type of flow presentation,
        the following types are possible:
        1. 'color' - raw color estimate [radians],
        2. 'doppler frequency' - color scaled in [kHz],
        3. 'power' - power estimate in [dB],
        4. 'speed' - color scaled in [mm/s],
    :param power_limit: (optional) flow estimate pixels corresponding to
                            power outside power_limit will not be shown,
    :param color_limit: (optional) two element tuple with color limit,
    :param bmode_limit: (optional) two element tuple with bmode limit.

    """

    def init():
        bimg.set_data(bmode[:, :, 0])
        fimg.set_data(doppler_img)
        return (bimg, fimg, )

    def animate(frame):
        doppler_img = prepare_doppler(
            doppler_array[:, :, frame],
            power_dB[:, :, frame],
            power_limit,
            doppler_type)

        bimg.set_data(bmode[:, :, frame])
        fimg.set_data(doppler_img)
        return (bimg, fimg, )

    if doppler_type == 'power':
        doppler_array = power_dB

    fig = plt.figure()
    fig.set_facecolor('white')
    bimg, fimg, doppler_img = show_flow(
        bmode[:, :, 0],
        doppler_array[:, :, 0],
        power_dB[:, :, 0],
        xgrid=xgrid,
        zgrid=zgrid,
        doppler_type=doppler_type,
        power_limit=power_limit,
        color_limit=color_limit,
        bmode_limit=bmode_limit)

    return animation.FuncAnimation(
        fig, animate, init_func=init, frames=bmode.shape[-1],
        interval=100, blit=True)



def filter_wall_clutter_cpu(input_signal, Wn=0.2, N=32):
    sos = signal.butter(N, Wn, 'high', output='sos')
    output_signal = signal.sosfiltfilt(sos, input_signal, axis=0)
    return output_signal.astype(np.complex64)


def filter_wall_clutter_gpu(input_signal, Wn=0.2, N=33):
    if N % 2 == 0:
        N = N+1
    b = signal.firwin(N, Wn, pass_zero=False)
    b = cp.array(b)
    output_signal = cupyx.scipy.ndimage.convolve1d(input_signal, b, axis=0)
    return output_signal.astype(np.complex64)

def show_cineloop(imgs, value_range=None, cmap=None, figsize=None,
                  interval=50, xlabel="Azimuth (mm)", ylabel="Depth (mm)",
                  extent=None):

    def init():
        img.set_data(imgs[0])
        return (img,)

    def animate(frame):
        img.set_data(imgs[frame])
        return (img,)

    fig, ax = plt.subplots()
    if figsize is not None:
        fig.set_size_inches(figsize)
    img = ax.imshow(imgs[0], cmap=cmap, extent=extent)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if value_range is not None:
        img.set_clim(*value_range)

    return animation.FuncAnimation(
        fig, animate, init_func=init, frames=len(imgs),
        interval=interval, blit=True)



