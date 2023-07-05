"""Implements VideoClip (base class for video clips) and its main subclasses:

- Animated clips:     VideoFileClip, ImageSequenceClip, BitmapClip
- Static image clips: ImageClip, ColorClip, TextClip,
"""

import copy as _copy
import os
import subprocess as sp
import tempfile
from numbers import Real

import numpy as np
import proglog
from imageio import imread, imsave
from PIL import Image, ImageDraw, ImageFont

from typing import List, Dict

from moviepy.Clip import Clip
from moviepy.decorators import (
    add_mask_if_none,
    apply_to_mask,
    convert_masks_to_RGB,
    convert_parameter_to_seconds,
    convert_path_to_string,
    outplace,
    requires_duration,
    requires_fps,
    use_clip_fps_by_default,
)
from moviepy.tools import (
    cross_platform_popen_params,
    extensions_dict,
    find_extension,
    subprocess_call,
)
from moviepy.video.io.ffmpeg_writer import ffmpeg_write_video
from moviepy.video.io.gif_writers import (
    write_gif_with_imageio,
)
from moviepy.video.tools.drawing import blit


class VideoClip(Clip):
    """Base class for video clips.

    See ``VideoFileClip``, ``ImageClip`` etc. for more user-friendly classes.


    Parameters
    ----------

    is_mask
      `True` if the clip is going to be used as a mask.


    Attributes
    ----------

    size
      The size of the clip, (width,height), in pixels.

    w, h
      The width and height of the clip, in pixels.

    is_mask
      Boolean set to `True` if the clip is a mask.

    make_frame
      A function ``t-> frame at time t`` where ``frame`` is a
      w*h*3 RGB array.

    mask (default None)
      VideoClip mask attached to this clip. If mask is ``None``,
                The video clip is fully opaque.

    audio (default None)
      An AudioClip instance containing the audio of the video clip.

    pos
      A function ``t->(x,y)`` where ``x,y`` is the position
      of the clip when it is composed with other clips.
      See ``VideoClip.set_pos`` for more details

    relative_pos
      See variable ``pos``.

    layer
      Indicates which clip is rendered on top when two clips overlap in
      a CompositeVideoClip. The highest number is rendered on top.
      Default is 0.

    """

    def __init__(
        self, make_frame=None, is_mask=False, duration=None, has_constant_size=True
    ):
        super().__init__()
        self.mask = None
        self.audio = None
        self.pos = lambda t: (0, 0)
        self.relative_pos = False
        self.layer = 0
        if make_frame:
            self.make_frame = make_frame
            self.size = self.get_frame(0).shape[:2][::-1]
        self.is_mask = is_mask
        self.has_constant_size = has_constant_size
        if duration is not None:
            self.duration = duration
            self.end = duration

    @property
    def w(self):
        """Returns the width of the video."""
        return self.size[0]

    @property
    def h(self):
        """Returns the height of the video."""
        return self.size[1]

    @property
    def aspect_ratio(self):
        """Returns the aspect ratio of the video."""
        return self.w / float(self.h)

    @property
    @requires_duration
    @requires_fps
    def n_frames(self):
        """Returns the number of frames of the video."""
        return int(self.duration * self.fps)

    def __copy__(self):
        """Mixed copy of the clip.

        Returns a shallow copy of the clip whose mask and audio will
        be shallow copies of the clip's mask and audio if they exist.

        This method is intensively used to produce new clips every time
        there is an outplace transformation of the clip (clip.resize,
        clip.with_subclip, etc.)

        Acts like a deepcopy except for the fact that readers and other
        possible unpickleables objects are not copied.
        """
        cls = self.__class__
        new_clip = cls.__new__(cls)
        for attr in self.__dict__:
            value = getattr(self, attr)
            if attr in ("mask", "audio"):
                value = _copy.copy(value)
            setattr(new_clip, attr, value)
        return new_clip

    copy = __copy__

    # ===============================================================
    # EXPORT OPERATIONS

    @convert_parameter_to_seconds(["t"])
    @convert_masks_to_RGB
    def save_frame(self, filename, t=0, with_mask=True):
        """Save a clip's frame to an image file.

        Saves the frame of clip corresponding to time ``t`` in ``filename``.
        ``t`` can be expressed in seconds (15.35), in (min, sec),
        in (hour, min, sec), or as a string: '01:03:05.35'.

        Parameters
        ----------

        filename : str
          Name of the file in which the frame will be stored.

        t : float or tuple or str, optional
          Moment of the frame to be saved. As default, the first frame will be
          saved.

        with_mask : bool, optional
          If is ``True`` the mask is saved in the alpha layer of the picture
          (only works with PNGs).
        """
        im = self.get_frame(t)
        if with_mask and self.mask is not None:
            mask = 255 * self.mask.get_frame(t)
            im = np.dstack([im, mask]).astype("uint8")
        else:
            im = im.astype("uint8")

        imsave(filename, im)

    @requires_duration
    @use_clip_fps_by_default
    @convert_masks_to_RGB
    @convert_path_to_string(["filename", "temp_audiofile", "temp_audiofile_path"])
    def write_videofile(
        self,
        filename,
        fps=None,
        codec=None,
        bitrate=None,
        audio=True,
        audio_fps=44100,
        preset="medium",
        audio_nbytes=4,
        audio_codec=None,
        audio_bitrate=None,
        audio_bufsize=2000,
        temp_audiofile=None,
        temp_audiofile_path="",
        remove_temp=True,
        write_logfile=False,
        threads=None,
        ffmpeg_params=None,
        logger="bar",
        pixel_format=None,
    ):
        """Write the clip to a videofile.

        Parameters
        ----------

        filename
          Name of the video file to write in, as a string or a path-like object.
          The extension must correspond to the "codec" used (see below),
          or simply be '.avi' (which will work with any codec).

        fps
          Number of frames per second in the resulting video file. If None is
          provided, and the clip has an fps attribute, this fps will be used.

        codec
          Codec to use for image encoding. Can be any codec supported
          by ffmpeg. If the filename is has extension '.mp4', '.ogv', '.webm',
          the codec will be set accordingly, but you can still set it if you
          don't like the default. For other extensions, the output filename
          must be set accordingly.

          Some examples of codecs are:

          - ``'libx264'`` (default codec for file extension ``.mp4``)
            makes well-compressed videos (quality tunable using 'bitrate').
          - ``'mpeg4'`` (other codec for extension ``.mp4``) can be an alternative
            to ``'libx264'``, and produces higher quality videos by default.
          - ``'rawvideo'`` (use file extension ``.avi``) will produce
            a video of perfect quality, of possibly very huge size.
          - ``png`` (use file extension ``.avi``) will produce a video
            of perfect quality, of smaller size than with ``rawvideo``.
          - ``'libvorbis'`` (use file extension ``.ogv``) is a nice video
            format, which is completely free/ open source. However not
            everyone has the codecs installed by default on their machine.
          - ``'libvpx'`` (use file extension ``.webm``) is tiny a video
            format well indicated for web videos (with HTML5). Open source.

        audio
          Either ``True``, ``False``, or a file name.
          If ``True`` and the clip has an audio clip attached, this
          audio clip will be incorporated as a soundtrack in the movie.
          If ``audio`` is the name of an audio file, this audio file
          will be incorporated as a soundtrack in the movie.

        audio_fps
          frame rate to use when generating the sound.

        temp_audiofile
          the name of the temporary audiofile, as a string or path-like object,
          to be created and then used to write the complete video, if any.

        temp_audiofile_path
          the location that the temporary audiofile is placed, as a
          string or path-like object. Defaults to the current working directory.

        audio_codec
          Which audio codec should be used. Examples are 'libmp3lame'
          for '.mp3', 'libvorbis' for 'ogg', 'libfdk_aac':'m4a',
          'pcm_s16le' for 16-bit wav and 'pcm_s32le' for 32-bit wav.
          Default is 'libmp3lame', unless the video extension is 'ogv'
          or 'webm', at which case the default is 'libvorbis'.

        audio_bitrate
          Audio bitrate, given as a string like '50k', '500k', '3000k'.
          Will determine the size/quality of audio in the output file.
          Note that it mainly an indicative goal, the bitrate won't
          necessarily be the this in the final file.

        preset
          Sets the time that FFMPEG will spend optimizing the compression.
          Choices are: ultrafast, superfast, veryfast, faster, fast, medium,
          slow, slower, veryslow, placebo. Note that this does not impact
          the quality of the video, only the size of the video file. So
          choose ultrafast when you are in a hurry and file size does not
          matter.

        threads
          Number of threads to use for ffmpeg. Can speed up the writing of
          the video on multicore computers.

        ffmpeg_params
          Any additional ffmpeg parameters you would like to pass, as a list
          of terms, like ['-option1', 'value1', '-option2', 'value2'].

        write_logfile
          If true, will write log files for the audio and the video.
          These will be files ending with '.log' with the name of the
          output file in them.

        logger
          Either ``"bar"`` for progress bar or ``None`` or any Proglog logger.

        pixel_format
          Pixel format for the output video file.

        Examples
        --------

        >>> from moviepy import VideoFileClip
        >>> clip = VideoFileClip("myvideo.mp4").with_subclip(100,120)
        >>> clip.write_videofile("my_new_video.mp4")
        >>> clip.close()

        """
        name, ext = os.path.splitext(os.path.basename(filename))
        ext = ext[1:].lower()
        logger = proglog.default_bar_logger(logger)

        if codec is None:
            try:
                codec = extensions_dict[ext]["codec"][0]
            except KeyError:
                raise ValueError(
                    "MoviePy couldn't find the codec associated "
                    "with the filename. Provide the 'codec' "
                    "parameter in write_videofile."
                )

        if audio_codec is None:
            if ext in ["ogv", "webm"]:
                audio_codec = "libvorbis"
            else:
                audio_codec = "libmp3lame"
        elif audio_codec == "raw16":
            audio_codec = "pcm_s16le"
        elif audio_codec == "raw32":
            audio_codec = "pcm_s32le"

        audiofile = audio if isinstance(audio, str) else None
        make_audio = (
            (audiofile is None) and (audio is True) and (self.audio is not None)
        )

        if make_audio and temp_audiofile:
            # The audio will be the clip's audio
            audiofile = temp_audiofile
        elif make_audio:
            audio_ext = find_extension(audio_codec)
            audiofile = os.path.join(
                temp_audiofile_path,
                name + Clip._TEMP_FILES_PREFIX + "wvf_snd.%s" % audio_ext,
            )

        # enough cpu for multiprocessing ? USELESS RIGHT NOW, WILL COME AGAIN
        # enough_cpu = (multiprocessing.cpu_count() > 1)
        logger(message="MoviePy - Building video %s." % filename)
        if make_audio:
            self.audio.write_audiofile(
                audiofile,
                audio_fps,
                audio_nbytes,
                audio_bufsize,
                audio_codec,
                bitrate=audio_bitrate,
                write_logfile=write_logfile,
                logger=logger,
            )

        ffmpeg_write_video(
            self,
            filename,
            fps,
            codec,
            bitrate=bitrate,
            preset=preset,
            write_logfile=write_logfile,
            audiofile=audiofile,
            threads=threads,
            ffmpeg_params=ffmpeg_params,
            logger=logger,
            pixel_format=pixel_format,
        )

        if remove_temp and make_audio:
            if os.path.exists(audiofile):
                os.remove(audiofile)
        logger(message="MoviePy - video ready %s" % filename)

    @requires_duration
    @use_clip_fps_by_default
    @convert_masks_to_RGB
    def write_images_sequence(
        self, name_format, fps=None, with_mask=True, logger="bar"
    ):
        """Writes the videoclip to a sequence of image files.

        Parameters
        ----------

        name_format
          A filename specifying the numerotation format and extension
          of the pictures. For instance "frame%03d.png" for filenames
          indexed with 3 digits and PNG format. Also possible:
          "some_folder/frame%04d.jpeg", etc.

        fps
          Number of frames per second to consider when writing the
          clip. If not specified, the clip's ``fps`` attribute will
          be used if it has one.

        with_mask
          will save the clip's mask (if any) as an alpha canal (PNGs only).

        logger
          Either ``"bar"`` for progress bar or ``None`` or any Proglog logger.


        Returns
        -------

        names_list
          A list of all the files generated.

        Notes
        -----

        The resulting image sequence can be read using e.g. the class
        ``ImageSequenceClip``.

        """
        logger = proglog.default_bar_logger(logger)
        # Fails on GitHub macos CI
        # logger(message="MoviePy - Writing frames %s." % name_format)

        timings = np.arange(0, self.duration, 1.0 / fps)

        filenames = []
        for i, t in logger.iter_bar(t=list(enumerate(timings))):
            name = name_format % i
            filenames.append(name)
            self.save_frame(name, t, with_mask=with_mask)
        # logger(message="MoviePy - Done writing frames %s." % name_format)

        return filenames

    @requires_duration
    @convert_masks_to_RGB
    @convert_path_to_string("filename")
    def write_gif(
        self,
        filename,
        fps=None,
        opt="nq",
        loop=0,
        colors=None,
        logger="bar",
    ):
        """Write the VideoClip to a GIF file.

        Converts a VideoClip into an animated GIF using imageio

        Parameters
        ----------

        filename
          Name of the resulting gif file, as a string or a path-like object.

        fps
          Number of frames per second (see note below). If it
          isn't provided, then the function will look for the clip's
          ``fps`` attribute (VideoFileClip, for instance, have one).

        opt
          Optimalization to apply. If program='imageio', opt must be either 'wu'
          (Wu) or 'nq' (Neuquant).

        loop : int, optional
          Repeat the clip using ``loop`` iterations in the resulting GIF.

        colors : int, optional
          Defines the maximum number of colors that the output image will have.

        progress_bar
          If True, displays a progress bar


        Notes
        -----

        The gif will be playing the clip in real time (you can
        only change the frame rate). If you want the gif to be played
        slower than the clip you will use ::

            >>> # slow down clip 50% and make it a gif
            >>> myClip.multiply_speed(0.5).to_gif('myClip.gif')

        """
        # A little sketchy at the moment, maybe move all that in write_gif,
        #  refactor a little... we will see.

        write_gif_with_imageio(
            self,
            filename,
            fps=fps,
            opt=opt,
            loop=loop,
            colors=colors,
            logger=logger,
        )

    # -----------------------------------------------------------------
    # F I L T E R I N G

    def subfx(self, fx, start_time=0, end_time=None, **kwargs):
        """Apply a transformation to a part of the clip.

        Returns a new clip in which the function ``fun`` (clip->clip)
        has been applied to the subclip between times `start_time` and `end_time`
        (in seconds).

        Examples
        --------

        >>> # The scene between times t=3s and t=6s in ``clip`` will be
        >>> # be played twice slower in ``new_clip``
        >>> new_clip = clip.subapply(lambda c:c.multiply_speed(0.5) , 3,6)

        """
        left = None if (start_time == 0) else self.with_subclip(0, start_time)
        center = self.with_subclip(start_time, end_time).fx(fx, **kwargs)
        right = None if (end_time is None) else self.with_subclip(start_time=end_time)

        clips = [clip for clip in [left, center, right] if clip is not None]

        # beurk, have to find other solution
        from moviepy.video.compositing.CompositeVideoClip import concatenate_videoclips

        return concatenate_videoclips(clips).with_start(self.start)

    # IMAGE FILTERS

    def image_transform(self, image_func, apply_to=None):
        """Modifies the images of a clip by replacing the frame `get_frame(t)` by
        another frame,  `image_func(get_frame(t))`.
        """
        apply_to = apply_to or []
        return self.transform(lambda get_frame, t: image_func(get_frame(t)), apply_to)

    # --------------------------------------------------------------
    # C O M P O S I T I N G

    def fill_array(self, pre_array, shape=(0, 0)):
        """TODO: needs documentation."""
        pre_shape = pre_array.shape
        dx = shape[0] - pre_shape[0]
        dy = shape[1] - pre_shape[1]
        post_array = pre_array
        if dx < 0:
            post_array = pre_array[: shape[0]]
        elif dx > 0:
            x_1 = [[[1, 1, 1]] * pre_shape[1]] * dx
            post_array = np.vstack((pre_array, x_1))
        if dy < 0:
            post_array = post_array[:, : shape[1]]
        elif dy > 0:
            x_1 = [[[1, 1, 1]] * dy] * post_array.shape[0]
            post_array = np.hstack((post_array, x_1))
        return post_array

    def blit_on(self, picture, t):
        """Returns the result of the blit of the clip's frame at time `t`
        on the given `picture`, the position of the clip being given
        by the clip's ``pos`` attribute. Meant for compositing.
        """
        wf, hf = picture.size

        ct = t - self.start  # clip time

        # GET IMAGE AND MASK IF ANY
        img = self.get_frame(ct).astype("uint8")
        im_img = Image.fromarray(img)

        if self.mask is not None:
            mask = (self.mask.get_frame(ct) * 255).astype("uint8")
            im_mask = Image.fromarray(mask).convert("L")

            if im_img.size != im_mask.size:
                bg_size = (
                    max(im_img.size[0], im_mask.size[0]),
                    max(im_img.size[1], im_mask.size[1]),
                )

                im_img_bg = Image.new("RGB", bg_size, "black")
                im_img_bg.paste(im_img, (0, 0))

                im_mask_bg = Image.new("L", bg_size, 0)
                im_mask_bg.paste(im_mask, (0, 0))

                im_img, im_mask = im_img_bg, im_mask_bg

        else:
            im_mask = None

        wi, hi = im_img.size
        # SET POSITION
        pos = self.pos(ct)

        # preprocess short writings of the position
        if isinstance(pos, str):
            pos = {
                "center": ["center", "center"],
                "left": ["left", "center"],
                "right": ["right", "center"],
                "top": ["center", "top"],
                "bottom": ["center", "bottom"],
            }[pos]
        else:
            pos = list(pos)

        # is the position relative (given in % of the clip's size) ?
        if self.relative_pos:
            for i, dim in enumerate([wf, hf]):
                if not isinstance(pos[i], str):
                    pos[i] = dim * pos[i]

        if isinstance(pos[0], str):
            D = {"left": 0, "center": (wf - wi) / 2, "right": wf - wi}
            pos[0] = D[pos[0]]

        if isinstance(pos[1], str):
            D = {"top": 0, "center": (hf - hi) / 2, "bottom": hf - hi}
            pos[1] = D[pos[1]]

        pos = map(int, pos)
        return blit(im_img, picture, pos, mask=im_mask)

    def with_add_mask(self):
        """Add a mask VideoClip to the VideoClip.

        Returns a copy of the clip with a completely opaque mask
        (made of ones). This makes computations slower compared to
        having a None mask but can be useful in many cases. Choose

        Set ``constant_size`` to  `False` for clips with moving
        image size.
        """
        if self.has_constant_size:
            mask = ColorClip(self.size, 1.0, is_mask=True)
            return self.with_mask(mask.with_duration(self.duration))
        else:

            def make_frame(t):
                return np.ones(self.get_frame(t).shape[:2], dtype=float)

            mask = VideoClip(is_mask=True, make_frame=make_frame)
            return self.with_mask(mask.with_duration(self.duration))

    def with_on_color(self, size=None, color=(0, 0, 0), pos=None, col_opacity=None):
        """Place the clip on a colored background.

        Returns a clip made of the current clip overlaid on a color
        clip of a possibly bigger size. Can serve to flatten transparent
        clips.

        Parameters
        ----------

        size
          Size (width, height) in pixels of the final clip.
          By default it will be the size of the current clip.

        color
          Background color of the final clip ([R,G,B]).

        pos
          Position of the clip in the final clip. 'center' is the default

        col_opacity
          Parameter in 0..1 indicating the opacity of the colored
          background.
        """
        from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip

        if size is None:
            size = self.size
        if pos is None:
            pos = "center"

        if col_opacity is not None:
            colorclip = ColorClip(
                size, color=color, duration=self.duration
            ).with_opacity(col_opacity)
            result = CompositeVideoClip([colorclip, self.with_position(pos)])
        else:
            result = CompositeVideoClip(
                [self.with_position(pos)], size=size, bg_color=color
            )

        if (
            isinstance(self, ImageClip)
            and (not hasattr(pos, "__call__"))
            and ((self.mask is None) or isinstance(self.mask, ImageClip))
        ):
            new_result = result.to_ImageClip()
            if result.mask is not None:
                new_result.mask = result.mask.to_ImageClip()
            return new_result.with_duration(result.duration)

        return result

    @outplace
    def with_make_frame(self, mf):
        """Change the clip's ``get_frame``.

        Returns a copy of the VideoClip instance, with the make_frame
        attribute set to `mf`.
        """
        self.make_frame = mf
        self.size = self.get_frame(0).shape[:2][::-1]

    @outplace
    def with_audio(self, audioclip):
        """Attach an AudioClip to the VideoClip.

        Returns a copy of the VideoClip instance, with the `audio`
        attribute set to ``audio``, which must be an AudioClip instance.
        """
        self.audio = audioclip

    @outplace
    def with_mask(self, mask):
        """Set the clip's mask.

        Returns a copy of the VideoClip with the mask attribute set to
        ``mask``, which must be a greyscale (values in 0-1) VideoClip.
        """
        assert mask is None or mask.is_mask
        self.mask = mask

    @add_mask_if_none
    @outplace
    def with_opacity(self, opacity):
        """Set the opacity/transparency level of the clip.

        Returns a semi-transparent copy of the clip where the mask is
        multiplied by ``op`` (any float, normally between 0 and 1).
        """
        self.mask = self.mask.image_transform(lambda pic: opacity * pic)

    @apply_to_mask
    @outplace
    def with_position(self, pos, relative=False):
        """Set the clip's position in compositions.

        Sets the position that the clip will have when included
        in compositions. The argument ``pos`` can be either a couple
        ``(x,y)`` or a function ``t-> (x,y)``. `x` and `y` mark the
        location of the top left corner of the clip, and can be
        of several types.

        Examples
        --------

        >>> clip.with_position((45,150)) # x=45, y=150
        >>>
        >>> # clip horizontally centered, at the top of the picture
        >>> clip.with_position(("center","top"))
        >>>
        >>> # clip is at 40% of the width, 70% of the height:
        >>> clip.with_position((0.4,0.7), relative=True)
        >>>
        >>> # clip's position is horizontally centered, and moving up !
        >>> clip.with_position(lambda t: ('center', 50+t) )

        """
        self.relative_pos = relative
        if hasattr(pos, "__call__"):
            self.pos = pos
        else:
            self.pos = lambda t: pos

    @apply_to_mask
    @outplace
    def with_layer(self, layer):
        """Set the clip's layer in compositions. Clips with a greater ``layer``
        attribute will be displayed on top of others.

        Note: Only has effect when the clip is used in a CompositeVideoClip.
        """
        self.layer = layer

    # --------------------------------------------------------------
    # CONVERSIONS TO OTHER TYPES

    @convert_parameter_to_seconds(["t"])
    def to_ImageClip(self, t=0, with_mask=True, duration=None):
        """
        Returns an ImageClip made out of the clip's frame at time ``t``,
        which can be expressed in seconds (15.35), in (min, sec),
        in (hour, min, sec), or as a string: '01:03:05.35'.
        """
        new_clip = ImageClip(self.get_frame(t), is_mask=self.is_mask, duration=duration)
        if with_mask and self.mask is not None:
            new_clip.mask = self.mask.to_ImageClip(t)
        return new_clip

    def to_mask(self, canal=0):
        """Return a mask a video clip made from the clip."""
        if self.is_mask:
            return self
        else:
            new_clip = self.image_transform(lambda pic: 1.0 * pic[:, :, canal] / 255)
            new_clip.is_mask = True
            return new_clip

    def to_RGB(self):
        """Return a non-mask video clip made from the mask video clip."""
        if self.is_mask:
            new_clip = self.image_transform(
                lambda pic: np.dstack(3 * [255 * pic]).astype("uint8")
            )
            new_clip.is_mask = False
            return new_clip
        else:
            return self

    # ----------------------------------------------------------------
    # Audio

    @outplace
    def without_audio(self):
        """Remove the clip's audio.

        Return a copy of the clip with audio set to None.
        """
        self.audio = None

    @outplace
    def afx(self, fun, *args, **kwargs):
        """Transform the clip's audio.

        Return a new clip whose audio has been transformed by ``fun``.
        """
        self.audio = self.audio.fx(fun, *args, **kwargs)

    def __add__(self, other):
        if isinstance(other, VideoClip):
            from moviepy.video.compositing.CompositeVideoClip import concatenate_videoclips

            method = "chain" if self.size == other.size else "compose"
            return concatenate_videoclips([self, other], method=method)
        return super(VideoClip, self).__add__(other)

    def __or__(self, other):
        """
        Implement the or (self | other) to produce a video with self and other
        placed side by side horizontally.
        """
        if isinstance(other, VideoClip):
            from moviepy.video.compositing.CompositeVideoClip import clips_array

            return clips_array([[self, other]])
        return super(VideoClip, self).__or__(other)

    def __truediv__(self, other):
        """
        Implement division (self / other) to produce a video with self
        placed on top of other.
        """
        if isinstance(other, VideoClip):
            from moviepy.video.compositing.CompositeVideoClip import clips_array

            return clips_array([[self], [other]])
        return super(VideoClip, self).__or__(other)

    def __matmul__(self, n):
        if not isinstance(n, Real):
            return NotImplemented
        from moviepy.video.fx.rotate import rotate

        return rotate(self, n)

    def __and__(self, mask):
        return self.with_mask(mask)


class DataVideoClip(VideoClip):
    """
    Class of video clips whose successive frames are functions
    of successive datasets

    Parameters
    ----------
    data
      A list of datasets, each dataset being used for one frame of the clip

    data_to_frame
      A function d -> video frame, where d is one element of the list `data`

    fps
      Number of frames per second in the animation
    """

    def __init__(self, data, data_to_frame, fps, is_mask=False, has_constant_size=True):
        self.data = data
        self.data_to_frame = data_to_frame
        self.fps = fps

        def make_frame(t):
            return self.data_to_frame(self.data[int(self.fps * t)])

        VideoClip.__init__(
            self,
            make_frame,
            is_mask=is_mask,
            duration=1.0 * len(data) / fps,
            has_constant_size=has_constant_size,
        )


class UpdatedVideoClip(VideoClip):
    """
    Class of clips whose make_frame requires some objects to
    be updated. Particularly practical in science where some
    algorithm needs to make some steps before a new frame can
    be generated.

    UpdatedVideoClips have the following make_frame:

    >>> def make_frame(t):
    >>>     while self.world.clip_t < t:
    >>>         world.update() # updates, and increases world.clip_t
    >>>     return world.to_frame()

    Parameters
    ----------

    world
      An object with the following attributes:
      - world.clip_t: the clip's time corresponding to the world's state.
      - world.update() : update the world's state, (including increasing
      world.clip_t of one time step).
      - world.to_frame() : renders a frame depending on the world's state.

    is_mask
      True if the clip is a WxH mask with values in 0-1

    duration
      Duration of the clip, in seconds

    """

    def __init__(self, world, is_mask=False, duration=None):
        self.world = world

        def make_frame(t):
            print(t)
            while self.world.clip_t < t:
                world.update()
            return world.to_frame()

        VideoClip.__init__(
            self, make_frame=make_frame, is_mask=is_mask, duration=duration
        )


"""---------------------------------------------------------------------

    ImageClip (base class for all 'static clips') and its subclasses
    ColorClip and TextClip.
    I would have liked to put these in a separate file but Python is bad
    at cyclic imports.

---------------------------------------------------------------------"""


class ImageClip(VideoClip):
    """Class for non-moving VideoClips.

    A video clip originating from a picture. This clip will simply
    display the given picture at all times.

    Examples
    --------

    >>> clip = ImageClip("myHouse.jpeg")
    >>> clip = ImageClip( someArray ) # a Numpy array represent

    Parameters
    ----------

    img
      Any picture file (png, tiff, jpeg, etc.) as a string or a path-like object,
      or any array representing an RGB image (for instance a frame from a VideoClip).

    is_mask
      Set this parameter to `True` if the clip is a mask.

    transparent
      Set this parameter to `True` (default) if you want the alpha layer
      of the picture (if it exists) to be used as a mask.

    Attributes
    ----------

    img
      Array representing the image of the clip.

    """

    def __init__(
        self, img, is_mask=False, transparent=True, fromalpha=False, duration=None
    ):
        VideoClip.__init__(self, is_mask=is_mask, duration=duration)

        if not isinstance(img, np.ndarray):
            # img is a string or path-like object, so read it in from disk
            img = imread(img)

        if len(img.shape) == 3:  # img is (now) a RGB(a) numpy array
            if img.shape[2] == 4:
                if fromalpha:
                    img = 1.0 * img[:, :, 3] / 255
                elif is_mask:
                    img = 1.0 * img[:, :, 0] / 255
                elif transparent:
                    self.mask = ImageClip(1.0 * img[:, :, 3] / 255, is_mask=True)
                    img = img[:, :, :3]
            elif is_mask:
                img = 1.0 * img[:, :, 0] / 255

        # if the image was just a 2D mask, it should arrive here
        # unchanged
        self.make_frame = lambda t: img
        self.size = img.shape[:2][::-1]
        self.img = img

    def transform(self, func, apply_to=None, keep_duration=True):
        """General transformation filter.

        Equivalent to VideoClip.transform. The result is no more an
        ImageClip, it has the class VideoClip (since it may be animated)
        """
        if apply_to is None:
            apply_to = []
        # When we use transform on an image clip it may become animated.
        # Therefore the result is not an ImageClip, just a VideoClip.
        new_clip = VideoClip.transform(
            self, func, apply_to=apply_to, keep_duration=keep_duration
        )
        new_clip.__class__ = VideoClip
        return new_clip

    @outplace
    def image_transform(self, image_func, apply_to=None):
        """Image-transformation filter.

        Does the same as VideoClip.image_transform, but for ImageClip the
        transformed clip is computed once and for all at the beginning,
        and not for each 'frame'.
        """
        if apply_to is None:
            apply_to = []
        arr = image_func(self.get_frame(0))
        self.size = arr.shape[:2][::-1]
        self.make_frame = lambda t: arr
        self.img = arr

        for attr in apply_to:
            a = getattr(self, attr, None)
            if a is not None:
                new_a = a.image_transform(image_func)
                setattr(self, attr, new_a)

    @outplace
    def time_transform(self, time_func, apply_to=None, keep_duration=False):
        """Time-transformation filter.

        Applies a transformation to the clip's timeline
        (see Clip.time_transform).

        This method does nothing for ImageClips (but it may affect their
        masks or their audios). The result is still an ImageClip.
        """
        if apply_to is None:
            apply_to = ["mask", "audio"]
        for attr in apply_to:
            a = getattr(self, attr, None)
            if a is not None:
                new_a = a.time_transform(time_func)
                setattr(self, attr, new_a)


class ColorClip(ImageClip):
    """An ImageClip showing just one color.

    Parameters
    ----------

    size
      Size (width, height) in pixels of the clip.

    color
      If argument ``is_mask`` is False, ``color`` indicates
      the color in RGB of the clip (default is black). If `is_mask``
      is True, ``color`` must be  a float between 0 and 1 (default is 1)

    is_mask
      Set to true if the clip will be used as a mask.

    """

    def __init__(self, size, color=None, is_mask=False, duration=None):
        w, h = size

        if is_mask:
            shape = (h, w)
            if color is None:
                color = 0
            elif not np.isscalar(color):
                raise Exception("Color has to be a scalar when mask is true")
        else:
            if color is None:
                color = (0, 0, 0)
            elif not hasattr(color, "__getitem__"):
                raise Exception("Color has to contain RGB of the clip")
            elif isinstance(color, str):
                raise Exception(
                    "Color cannot be string. Color has to contain RGB of the clip"
                )
            shape = (h, w, len(color))

        super().__init__(
            np.tile(color, w * h).reshape(shape), is_mask=is_mask, duration=duration
        )


class TextClip(ImageClip):
        """Class for autogenerated text clips.

        Creates an ImageClip originating from a script-generated text image.

        Parameters
        ----------

        font
          Path to the font to use. Must be an OpenType font.
          See ``TextClip.list('font')`` for the list of fonts you can use on
          your computer.

        text
          A string of the text to write. Can be replaced by argument
          ``filename``.

        filename
          The name of a file in which there is the text to write,
          as a string or a path-like object.
          Can be provided instead of argument ``text``

        font_size
          Font size in point. Can be auto-set if method='caption',
          or if method='label' and size is set.

        size
          Size of the picture in pixels. Can be auto-set if
          method='label' and font_size is set, but mandatory if method='caption'.
          the height can be None for caption if font_size is defined,
          it will then be auto-determined.

        bg_color
          Color of the background. Default to None for no background. Can be
          a RGB (or RGBA if transparent = ``True``) ``tuple``, a color name, or an
          hexadecimal notation.

        color
          Color of the text. Default to "black". Can be
          a RGB (or RGBA if transparent = ``True``) ``tuple``, a color name, or an
          hexadecimal notation.

        
        stroke_color
          Color of the stroke (=contour line) of the text. If ``None``,
          there will be no stroke.

        stroke_width
          Width of the stroke, in pixels. Can be a float, like 1.5.

        method
          Either 'label' (default, the picture will be autosized so as to fit
          exactly the size) or 'caption' (the text will be drawn in a picture
          with fixed size provided with the ``size`` argument). If `caption`,
          the text will be wrapped automagically.

        text_align
          center | left | right. Text align similar to css. Default to ``center``.

        horizontal_align
          center | left | right. Define horizontal align of text bloc in image.
          Default to ``center``.

        vertical_align
          center | top | bottom. Define vertical align of text bloc in image. 
          Default to ``center``.

        interline
          Interline spacing. Default to ``4``.

        transparent
          ``True`` (default) if you want to take into account the
          transparency in the image.

        duration
            Duration of the clip
        """

        @convert_path_to_string("filename")
        def __init__(
            self,
            font,
            text=None,
            filename=None,
            font_size=None,
            size=(None, None),
            color="black",
            bg_color=None,
            stroke_color=None,
            stroke_width=0,
            method="label",
            text_align="left",
            horizontal_align="center",
            vertical_align="center",
            interline=4,
            transparent=True,
            duration=None
        ):
            
            def break_text (width, text, font, font_size, stroke_width, align, spacing) -> List[str] :
                """
                    Break text to never overflow a width
                """
                img = Image.new("RGB", (1, 1))
                font_pil = ImageFont.truetype(font, font_size)
                draw = ImageDraw.Draw(img)

                lines = []
                current_line = ''
                words = text.split(' ')
                for word in words:
                    temp_line = (current_line + ' ' + word if current_line else word)
                    temp_left, temp_top, temp_right, temp_bottom = draw.multiline_textbbox((0,0), temp_line, font=font_pil, spacing=spacing, 
                        align=align, stroke_width=stroke_width)
                    temp_width = temp_right - temp_left

                    if temp_width <= width:
                        current_line = temp_line
                    else:
                        lines.append(current_line)
                        current_line = word

                if current_line :
                    lines.append(current_line)

                return lines

            def find_text_size (text, font, font_size, stroke_width, align, spacing, max_width=None, allow_break=False) -> tuple[int, int] :
                """ 
                    Find dimensions a text will occupy
                    return a tuple (width, height)
                """
                img = Image.new("RGB", (1, 1))
                font_pil = ImageFont.truetype(font, font_size)
                draw = ImageDraw.Draw(img)

                if max_width is None or not allow_break:
                    left, top, right, bottom = draw.multiline_textbbox((0,0), text, font=font_pil, spacing=spacing, 
                        align=align, stroke_width=stroke_width, anchor="lm")
                    
                    return (int(right - left), int(bottom - top))
                
                lines = break_text(width=max_width, text=text, font=font, font_size=font_size, stroke_width=stroke_width,
                                   align=align, spacing=spacing)

                left, top, right, bottom = draw.multiline_textbbox((0,0), "\n".join(lines), font=font_pil, spacing=spacing, 
                        align=align, stroke_width=stroke_width, anchor="lm")
                    
                return (int(right - left), int(bottom - top))

            def find_optimum_font_size (text, font, stroke_width, align, spacing, width, height=None, allow_break=False) :
                """
                    Find the best font size to fit as optimally as possible
                """
                max_font_size = width
                min_font_size = 1

                # Try find best size using bisection
                while min_font_size < max_font_size:
                    avg_font_size = int((max_font_size + min_font_size) // 2)
                    text_width, text_height = find_text_size(text, font, avg_font_size, stroke_width, align, spacing, 
                                                             max_width=width, allow_break=allow_break)

                    if text_width <= width and (height is None or text_height <= height):
                        min_font_size = avg_font_size + 1
                    else:
                        max_font_size = avg_font_size - 1

                # Check if the last font size tested fits within the given width and height
                text_width, text_height = find_text_size(text, font, min_font_size, stroke_width, align, spacing,
                                                         max_width=width, allow_break=allow_break)
                if text_width <= width and (height is None or text_height <= height):
                    return min_font_size
                else:
                    return min_font_size - 1

            try :
                _ = ImageFont.truetype(font)
            except Exception as e :
                raise ValueError("Invalid font {}, pillow failed to use it with error {}".format(font, e))

            if filename:
                with open(filename, 'r') as file:
                    text = file.read().rstrip() # Remove newline at end

            if text is None :
                raise ValueError("No text nor filename provided")
            
            # Compute all img and text sizes if some are missing
            img_width, img_height = size

            if method == "caption" :
                if img_width is None :
                    raise ValueError("Size is mandatory when method is caption")
            
                if img_height is None and font_size is None :
                    raise ValueError("Height is mandatory when method is caption and font size is None")

                if font_size is None :
                    font_size = find_optimum_font_size(text=text, font=font, stroke_width=stroke_width, align=text_align, 
                                                       spacing=interline, width=img_width, height=img_height, allow_break=True)
                
                if img_height is None :
                    img_height = find_text_size(text=text, font=font, font_size=font_size, stroke_width=stroke_width,
                                                align=text_align, spacing=interline, max_width=img_width, allow_break=True)[1]

                # Add line breaks whenever needed
                text = "\n".join(break_text(width=img_width, text=text, font=font, font_size=font_size, stroke_width=stroke_width, 
                                            align=text_align, spacing=interline))
                    
            elif method == "label" :
                if font_size is None and img_width is None :
                    raise ValueError("Font size is mandatory when method is label and size is None")
            
                if font_size is None :
                    font_size = find_optimum_font_size(text=text, font=font, stroke_width=stroke_width, align=text_align, 
                                                       spacing=interline, width=img_width, height=img_height)
                    
                if img_width is None :
                    img_width = find_text_size(text=text, font=font, font_size=font_size, stroke_width=stroke_width,
                                                align=text_align, spacing=interline)[0]
                    
                if img_height is None :
                    img_height = find_text_size(text=text, font=font, font_size=font_size, stroke_width=stroke_width,
                                                align=text_align, spacing=interline, max_width=img_width)[1]
                                        
            else :
                raise ValueError("Method must be either `caption` or `label`.")
            
            # Trace the image
            img_mode = "RGBA" if transparent else "RGB"
            img = Image.new(img_mode, (img_width, img_height), color=bg_color)
            pil_font = ImageFont.truetype(font, font_size)
            draw = ImageDraw.Draw(img)

            # Dont need allow break here, because we already breaked in caption
            text_width, text_height = find_text_size(text=text, font=font, font_size=font_size, stroke_width=stroke_width,
                                                align=text_align, spacing=interline, max_width=img_width)
            
            x = 0
            if horizontal_align == "right" :
                x = img_width - text_width
            elif horizontal_align == "center" :
                x = (img_width - text_width) / 2

            y = 0
            if vertical_align == "bottom" :
                y = img_height - text_height
            elif vertical_align == "center" :
                y = (img_height - text_height) / 2


            # So, pillow multiline support is horrible, in particular multiline_text and multiline_textbbox
            # are not intuitive at all. They cannot use left top (see https://pillow.readthedocs.io/en/stable/handbook/text-anchors.html)
            # as anchor, so we always have to use left middle instead. Else we would always have a useless
            # margin (the diff between ascender and top) on any text.
            # That mean our Y is actually not from 0 for top, but need to be increment by half our text height,
            # since we have to reference from middle line.
            y += text_height / 2

            draw.multiline_text(xy=(x, y), text=text, fill=color, font=pil_font, spacing=interline, align=text_align, stroke_width=stroke_width, 
                      stroke_fill=stroke_color, anchor="lm")
            
            # We just need the image as a numpy array
            img_numpy = np.array(img)

            ImageClip.__init__(self, img=img_numpy, transparent=transparent, duration=duration)
            self.text = text
            self.color = color
            self.stroke_color = stroke_color


class BitmapClip(VideoClip):
    """Clip made of color bitmaps. Mainly designed for testing purposes."""

    DEFAULT_COLOR_DICT = {
        "R": (255, 0, 0),
        "G": (0, 255, 0),
        "B": (0, 0, 255),
        "O": (0, 0, 0),
        "W": (255, 255, 255),
        "A": (89, 225, 62),
        "C": (113, 157, 108),
        "D": (215, 182, 143),
        "E": (57, 26, 252),
        "F": (225, 135, 33),
    }

    @convert_parameter_to_seconds(["duration"])
    def __init__(
        self, bitmap_frames, *, fps=None, duration=None, color_dict=None, is_mask=False
    ):
        """Creates a VideoClip object from a bitmap representation. Primarily used
        in the test suite.

        Parameters
        ----------

        bitmap_frames
          A list of frames. Each frame is a list of strings. Each string
          represents a row of colors. Each color represents an (r, g, b) tuple.
          Example input (2 frames, 5x3 pixel size)::

              [["RRRRR",
                "RRBRR",
                "RRBRR"],
               ["RGGGR",
                "RGGGR",
                "RGGGR"]]

        fps
          The number of frames per second to display the clip at. `duration` will
          calculated from the total number of frames. If both `fps` and `duration`
          are set, `duration` will be ignored.

        duration
          The total duration of the clip. `fps` will be calculated from the total
          number of frames. If both `fps` and `duration` are set, `duration` will
          be ignored.

        color_dict
          A dictionary that can be used to set specific (r, g, b) values that
          correspond to the letters used in ``bitmap_frames``.
          eg ``{"A": (50, 150, 150)}``.

          Defaults to::

              {
                "R": (255, 0, 0),
                "G": (0, 255, 0),
                "B": (0, 0, 255),
                "O": (0, 0, 0),  # "O" represents black
                "W": (255, 255, 255),
                # "A", "C", "D", "E", "F" represent arbitrary colors
                "A": (89, 225, 62),
                "C": (113, 157, 108),
                "D": (215, 182, 143),
                "E": (57, 26, 252),
              }

        is_mask
          Set to ``True`` if the clip is going to be used as a mask.
        """
        assert fps is not None or duration is not None

        self.color_dict = color_dict if color_dict else self.DEFAULT_COLOR_DICT

        frame_list = []
        for input_frame in bitmap_frames:
            output_frame = []
            for row in input_frame:
                output_frame.append([self.color_dict[color] for color in row])
            frame_list.append(np.array(output_frame))

        frame_array = np.array(frame_list)
        self.total_frames = len(frame_array)

        if fps is None:
            fps = self.total_frames / duration
        else:
            duration = self.total_frames / fps

        VideoClip.__init__(
            self,
            make_frame=lambda t: frame_array[int(t * fps)],
            is_mask=is_mask,
            duration=duration,
        )
        self.fps = fps

    def to_bitmap(self, color_dict=None):
        """Returns a valid bitmap list that represents each frame of the clip.
        If `color_dict` is not specified, then it will use the same `color_dict`
        that was used to create the clip.
        """
        color_dict = color_dict or self.color_dict

        bitmap = []
        for frame in self.iter_frames():
            bitmap.append([])
            for line in frame:
                bitmap[-1].append("")
                for pixel in line:
                    letter = list(color_dict.keys())[
                        list(color_dict.values()).index(tuple(pixel))
                    ]
                    bitmap[-1][-1] += letter

        return bitmap
