#
# This file is part of the v4l2py project
#
# Copyright (c) 2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

import os
import enum
import mmap
import errno
import fcntl
import select
import logging
import pathlib
import fractions
import collections
import copy
import ctypes

from . import raw


log = logging.getLogger(__name__)


def _enum(name, prefix, klass=enum.IntEnum):
    return klass(
        name,
        (
            (name.replace(prefix, ""), getattr(raw, name))
            for name in dir(raw)
            if name.startswith(prefix)
        ),
    )


Capability = _enum("Capability", "V4L2_CAP_", klass=enum.IntFlag)
PixelFormat = _enum("PixelFormat", "V4L2_PIX_FMT_")
BufferType = _enum("BufferType", "V4L2_BUF_TYPE_")
Memory = _enum("Memory", "V4L2_MEMORY_")
ImageFormatFlag = _enum("ImageFormatFlag", "V4L2_FMT_FLAG_", klass=enum.IntFlag)
Field = _enum("Field", "V4L2_FIELD_")
FrameSizeType = _enum("FrameSizeType", "V4L2_FRMSIZE_TYPE_")
FrameIntervalType = _enum("FrameIntervalType", "V4L2_FRMIVAL_TYPE_")
IOC = _enum("IOC", "VIDIOC_", klass=enum.Enum)


Info = collections.namedtuple(
    "Info",
    "driver card bus_info version physical_capabilities capabilities crop_capabilities buffers formats frame_sizes",
)

ImageFormat = collections.namedtuple(
    "ImageFormat", "type description flags pixel_format"
)

Format = collections.namedtuple("Format", "width height pixel_format")

CropCapability = collections.namedtuple(
    "CropCapability", "type bounds defrect pixel_aspect"
)

Rect = collections.namedtuple("Rect", "left top width height")

Size = collections.namedtuple("Size", "width height")

FrameType = collections.namedtuple(
    "FrameType", "type pixel_format width height min_fps max_fps step_fps"
)


INFO_REPR = """\
driver = {info.driver}
card = {info.card}
bus = {info.bus_info}
version = {info.version}
physical capabilities = {physical_capabilities}
capabilities = {capabilities}
buffers = {buffers}
"""


def flag_items(flag):
    return [item for item in type(flag) if item in flag]


def Info_repr(info):
    caps = "|".join(cap.name for cap in flag_items(info.capabilities))
    pcaps = "|".join(cap.name for cap in flag_items(info.physical_capabilities))
    buffers = "|".join(buff.name for buff in info.buffers)
    return INFO_REPR.format(
        info=info, capabilities=caps, physical_capabilities=pcaps, buffers=buffers
    )


Info.__repr__ = Info_repr


def frame_sizes(fd, pixel_formats):
    def get_frame_intervals(fmt, w, h):
        val = raw.v4l2_frmivalenum()
        val.pixel_format = fmt
        val.width = w
        val.height = h
        res = []
        for index in range(128):
            try:
                fcntl.ioctl(fd, IOC.ENUM_FRAMEINTERVALS.value, val)
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                else:
                    raise
            val.index = index
            # values come in frame interval (fps = 1/interval)
            try:
                ftype = FrameIntervalType(val.type)
            except ValueError:
                break
            if ftype == FrameIntervalType.DISCRETE:
                min_fps = max_fps = step_fps = fractions.Fraction(
                    val.discrete.denominator / val.discrete.numerator
                )
            else:
                if val.stepwise.min.numerator == 0:
                    min_fps = 0
                else:
                    min_fps = fractions.Fraction(
                        val.stepwise.min.denominator, val.stepwise.min.numerator
                    )
                if val.stepwise.max.numerator == 0:
                    max_fps = 0
                else:
                    max_fps = fractions.Fraction(
                        val.stepwise.max.denominator, val.stepwise.max.numerator
                    )
                if val.stepwise.step.numerator == 0:
                    step_fps = 0
                else:
                    step_fps = fractions.Fraction(
                        val.stepwise.step.denominator, val.stepwise.step.numerator
                    )
            res.append(
                FrameType(
                    type=ftype,
                    pixel_format=fmt,
                    width=w,
                    height=h,
                    min_fps=min_fps,
                    max_fps=max_fps,
                    step_fps=step_fps,
                )
            )
        return res

    size = raw.v4l2_frmsizeenum()
    sizes = []
    for pixel_format in pixel_formats:
        size.pixel_format = pixel_format
        fcntl.ioctl(fd, IOC.ENUM_FRAMESIZES.value, size)
        if size.type == FrameSizeType.DISCRETE:
            sizes += get_frame_intervals(
                pixel_format, size.discrete.width, size.discrete.height
            )
    return sizes


def read_capabilities(fd):
    caps = raw.v4l2_capability()
    fcntl.ioctl(fd, IOC.QUERYCAP.value, caps)
    return caps


def read_info(fd):
    caps = read_capabilities(fd)
    version_tuple = (
        (caps.version & 0xFF0000) >> 16,
        (caps.version & 0x00FF00) >> 8,
        (caps.version & 0x0000FF),
    )
    version_str = ".".join(map(str, version_tuple))
    device_capabilities = Capability(caps.device_caps)
    buffers = [typ for typ in BufferType if Capability[typ.name] in device_capabilities]

    fmt = raw.v4l2_fmtdesc()
    img_fmt_stream_types = {
        BufferType.VIDEO_CAPTURE,
        BufferType.VIDEO_CAPTURE_MPLANE,
        BufferType.VIDEO_OUTPUT,
        BufferType.VIDEO_OUTPUT_MPLANE,
        BufferType.VIDEO_OVERLAY,
    } & set(buffers)

    formats = []
    pixel_formats = set()
    for stream_type in img_fmt_stream_types:
        fmt.type = stream_type
        for index in range(128):
            fmt.index = index
            try:
                fcntl.ioctl(fd, IOC.ENUM_FMT.value, fmt)
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                else:
                    raise
            try:
                pixel_format = PixelFormat(fmt.pixelformat)
            except ValueError:
                continue
            formats.append(
                ImageFormat(
                    type=stream_type,
                    flags=ImageFormatFlag(fmt.flags),
                    description=fmt.description.decode(),
                    pixel_format=pixel_format,
                )
            )
            pixel_formats.add(pixel_format)

    crop = raw.v4l2_cropcap()
    crop_stream_types = {
        BufferType.VIDEO_CAPTURE,
        BufferType.VIDEO_OUTPUT,
        BufferType.VIDEO_OVERLAY,
    } & set(buffers)
    crop_caps = []
    for stream_type in crop_stream_types:
        crop.type = stream_type
        try:
            fcntl.ioctl(fd, IOC.CROPCAP.value, crop)
        except OSError:
            continue
        crop_caps.append(
            CropCapability(
                type=stream_type,
                bounds=Rect(
                    crop.bounds.left,
                    crop.bounds.top,
                    crop.bounds.width,
                    crop.bounds.height,
                ),
                defrect=Rect(
                    crop.defrect.left,
                    crop.defrect.top,
                    crop.defrect.width,
                    crop.defrect.height,
                ),
                pixel_aspect=crop.pixelaspect.numerator / crop.pixelaspect.denominator,
            )
        )

    return Info(
        driver=caps.driver.decode(),
        card=caps.card.decode(),
        bus_info=caps.bus_info.decode(),
        version=version_str,
        physical_capabilities=Capability(caps.capabilities),
        capabilities=device_capabilities,
        crop_capabilities=crop_caps,
        buffers=buffers,
        formats=formats,
        frame_sizes=frame_sizes(fd, pixel_formats),
    )


def fopen(path, rw=False):
    return open(path, "rb+" if rw else "rb", buffering=0, opener=opener)


def opener(path, flags):
    return os.open(path, flags | os.O_NONBLOCK)


class Device:
    def __init__(self, filename):
        filename = pathlib.Path(filename)
        self._log = log.getChild(filename.stem)
        self._context_level = 0
        self._fobj = fopen(filename, rw=True)
        self.info = read_info(self.fileno())
        self.filename = filename
        if Capability.VIDEO_CAPTURE in self.info.capabilities:
            self.video_capture = VideoCapture(self)
        else:
            self.video_capture = None
        self.ctrl_list = self._get_device_controls()

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def __iter__(self):
        return iter(self.video_capture)

    def _ioctl(self, request, arg=0):
        return fcntl.ioctl(self, request, arg)

    @classmethod
    def from_id(self, did):
        return Device("/dev/video{}".format(did))

    def close(self):
        if not self.closed:
            self._log.info("closing")
            self._fobj.close()

    def fileno(self):
        return self._fobj.fileno()

    @property
    def closed(self):
        return self._fobj.closed

    def _get_device_controls(self):
        ret = []
        queryctrl_ext = raw.v4l2_query_ext_ctrl()
        queryctrl_ext.id = (
            raw.V4L2_CTRL_FLAG_NEXT_CTRL | raw.V4L2_CTRL_FLAG_NEXT_COMPOUND
        )

        while True:
            try:
                self._ioctl(IOC.QUERY_EXT_CTRL.value, queryctrl_ext)
            except OSError as e:
                assert e.errno == errno.EINVAL
                break
            #print(f"name={queryctrl_ext.name}")

            if not (queryctrl_ext.flags & raw.V4L2_CTRL_FLAG_DISABLED) and not (queryctrl_ext.type == raw.V4L2_CTRL_TYPE_CTRL_CLASS):
                ret.append(copy.deepcopy(queryctrl_ext))
            queryctrl_ext.id |= (raw.V4L2_CTRL_FLAG_NEXT_CTRL | raw.V4L2_CTRL_FLAG_NEXT_COMPOUND)

        return ret

    def ctrls_name(self):
        ret = []
        for queryctrl in self.ctrl_list:
            ret.append(queryctrl.name.decode("UTF-8"))
        return ret

    def get_ctrl(self, name):
        for queryctrl in self.ctrl_list:
            if queryctrl.name.decode("UTF-8").lower() != name.lower():
                continue
            if queryctrl.flags & raw.V4L2_CTRL_FLAG_HAS_PAYLOAD:
                #print(f"has payload elems:{queryctrl.elems} elem_size:{queryctrl.elem_size}")
                controls = raw.v4l2_ext_controls()
                controls.count = 1
                
                control = raw.v4l2_ext_control()
                control.id = queryctrl.id
                if queryctrl.type == raw.V4L2_CTRL_TYPE_STRING:
                    control.size = queryctrl.elems * queryctrl.elem_size + 1
                else:
                    control.size = queryctrl.elems * queryctrl.elem_size
                data = ctypes.create_string_buffer(control.size)
                control.ptr = ctypes.cast(ctypes.pointer(data), ctypes.c_void_p)
                
                controls.controls = ctypes.pointer(control)
                try:
               	    self._ioctl(IOC.G_EXT_CTRLS.value, controls)
                except OSError as error:
            	    #print(error.errno)
            	    raise
            	
                if queryctrl.type == raw.V4L2_CTRL_TYPE_STRING:
                    return ctypes.cast(control.string, ctypes.c_char_p).value.decode("UTF-8")
            	
                #print(f"size:{control.size} elems:{queryctrl.elems} elem_size:{queryctrl.elem_size}")
                n_elems = int(control.size/queryctrl.elem_size)
                if queryctrl.elem_size == 1:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint8))
                elif queryctrl.elem_size == 2:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint16))
                elif queryctrl.elem_size == 4:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint32))
                else:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint8))
                    n_elems = control.size
                
                #for i in range(0, n_elems):
                #    print(arr[i])
                return arr[:n_elems]
            else:
                control = raw.v4l2_control(queryctrl.id)
                try:
               	    self._ioctl(IOC.G_CTRL.value, control)
                except OSError as error:
            	    #print(error.errno)
            	    raise
                return control.value            
        raise ValueError("Failed to find control %s" % name)
        
    def ctrl_to_string(self, ctrl):
        if isinstance(ctrl, raw.v4l2_control):
            return f"v4l2_control: val={ctrl.value}"
        elif isinstance(ctrl, raw.v4l2_ext_control):
            print(f"v4l2_ext_control: size={ctrl.size}")
        else:
            return "Error, ctrl must be 'v4l2_control' or 'v4l2_ext_control'"

    def set_ctrl(self, name, value):
        for queryctrl in self.ctrl_list:
            if queryctrl.name.decode("UTF-8").lower() != name.lower():
                continue
                
            if queryctrl.flags & raw.V4L2_CTRL_FLAG_HAS_PAYLOAD:
                controls = raw.v4l2_ext_controls()
                controls.count = 1
                
                control = raw.v4l2_ext_control()
                control.id = queryctrl.id
                if queryctrl.type == raw.V4L2_CTRL_TYPE_STRING:
                    control.size = queryctrl.elems * queryctrl.elem_size + 1
                    #TODO
                    return
                else:
                    control.size = queryctrl.elems * queryctrl.elem_size
                
                data = ctypes.create_string_buffer(control.size)
                control.ptr = ctypes.cast(ctypes.pointer(data), ctypes.c_void_p)
                
                n_elems = queryctrl.elems
                if queryctrl.elem_size == 1:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint8))
                elif queryctrl.elem_size == 2:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint16))
                elif queryctrl.elem_size == 4:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint32))
                else:
                    arr = ctypes.cast(control.ptr, ctypes.POINTER(ctypes.c_uint8))
                    n_elems *= queryctrl.elem_size
                
                if len(value) > n_elems:
                    raise ValueError(f"Incompatible sizes: {len(value)} > n_elems:{n_elems} ({queryctrl.elems}*{queryctrl.elem_size})")
                
                for i in range(0, n_elems):
                    if i >= len(value):
                        break
                    if value[i] < queryctrl.minimum or value[i] > queryctrl.maximum:
                        raise ValueError(
                            "Require %d <= %d <= %d"
                            % (queryctrl.minimum, value[i], queryctrl.maximum)
                        )
                    arr[i] = value[i]
                
                controls.controls = ctypes.pointer(control)
                try:
               	    self._ioctl(IOC.S_EXT_CTRLS.value, controls)
                except OSError as error:
            	    #print(error.errno)
            	    raise
                return
            else:
                if value < queryctrl.minimum or value > queryctrl.maximum:
                    raise ValueError(
                        "Require %d <= %d <= %d"
                        % (queryctrl.minimum, value, queryctrl.maximum)
                    )
                
                control = raw.v4l2_control(queryctrl.id, value)
                try:
                    self._ioctl(IOC.S_CTRL.value, control)
                except OSError as error:
                    #print(error.errno)
                    raise
                return
        raise ValueError("Failed to find control %s" % name)


class VideoCapture:

    buffer_type = BufferType.VIDEO_CAPTURE
    
    MAX_NR_CROP_RECTS = 8

    def __init__(self, device):
        self.device = device

    def __iter__(self):
        return iter(VideoStream(self))

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request.value, arg=arg)

    @property
    def formats(self):
        return [fmt for fmt in self.device.info.formats if fmt.type == self.buffer_type]

    @property
    def crop_capabilities(self):
        return [
            crop
            for crop in self.device.info.crop_capabilities
            if crop.type == self.buffer_type
        ]

    def set_format(self, width, height, pixel_format="MJPG"):
        f = raw.v4l2_format()
        if isinstance(pixel_format, str):
            pixel_format = raw.v4l2_fourcc(*pixel_format.upper())
        f.type = self.buffer_type
        f.fmt.pix.pixelformat = pixel_format
        f.fmt.pix.field = Field.ANY
        f.fmt.pix.width = width
        f.fmt.pix.height = height
        f.fmt.pix.bytesperline = 0
        try:
            return self._ioctl(IOC.S_FMT, f)
        except OSError as error:
            #print(error.errno)
            raise

    def get_format(self):
        f = raw.v4l2_format()
        f.type = self.buffer_type
        try:
            self._ioctl(IOC.G_FMT, f)
        except OSError as error:
            #print(error.errno)
            raise
        return Format(
            width=f.fmt.pix.width,
            height=f.fmt.pix.height,
            pixel_format=PixelFormat(f.fmt.pix.pixelformat),
        )

    def set_fps(self, fps):
        p = raw.v4l2_streamparm()
        p.type = self.buffer_type
        max_den = int(min(2**32, 2**32/fps)) #v4l2 fraction is u32
        fps = fractions.Fraction(fps).limit_denominator(max_den)
        p.parm.capture.timeperframe.numerator = fps.denominator
        p.parm.capture.timeperframe.denominator = fps.numerator
        #print(fps)
        try:
            return self._ioctl(IOC.S_PARM, p)
        except OSError as error:
            #print(error.errno)
            raise

    def get_fps(self):
        p = raw.v4l2_streamparm()
        p.type = self.buffer_type
        try:
            self._ioctl(IOC.G_PARM, p)
        except OSError as error:
            #print(error.errno)
            raise
        return (
            p.parm.capture.timeperframe.denominator
            / p.parm.capture.timeperframe.numerator
        )

    def set_crop(self, left, top, width, height):
        crop = raw.v4l2_crop()
        crop.type = self.buffer_type
        rect = raw.v4l2_rect()
        rect.left = left
        rect.top = top
        rect.width = width
        rect.height = height
        crop.c = rect
        try:
            return self._ioctl(IOC.S_CROP, crop)
        except OSError as error:
            #print(error.errno)
            raise

    def get_crop(self):
        crop = raw.v4l2_crop()
        crop.type = self.buffer_type
        try:
            self._ioctl(IOC.G_CROP, crop)
        except OSError as error:
            #print(error.errno)
            raise
        return Rect(
            left=crop.c.left, top=crop.c.top, width=crop.c.width, height=crop.c.height
        )
        
    def get_selection(self):
        selection = raw.v4l2_selection()
        selection.type = self.buffer_type
        selection.rectangles = VideoCapture.MAX_NR_CROP_RECTS
        rects = (raw.v4l2_ext_rect * selection.rectangles)()
        selection.pr = ctypes.cast(ctypes.pointer(rects), ctypes.POINTER(raw.v4l2_ext_rect))
        try:
            self._ioctl(IOC.G_SELECTION, selection)
        except OSError as error:
            #print(error.errno)
            raise
        if selection.rectangles == 0:
            return Rect(
                left=selection.r.left, top=selection.r.top, width=selection.r.width, height=selection.r.height
            )
        else:
            res = []
            for i in range(0, selection.rectangles):
                r = Rect(
                    left=rects[i].r.left, top=rects[i].r.top, width=rects[i].r.width, height=rects[i].r.height
                )
                res.append(r)
            return res
            
    def set_selection(self, rect_array):
        nr_rects = len(rect_array)
        if nr_rects > VideoCapture.MAX_NR_CROP_RECTS:
            raise ValueError(f"Too many selection areas: {nr_rects}, max supported: {VideoCapture.MAX_NR_CROP_RECTS}")
            
        selection = raw.v4l2_selection()
        selection.type = self.buffer_type
        selection.target = raw.V4L2_SEL_TGT_CROP
        selection.rectangles = nr_rects
        rects = (raw.v4l2_ext_rect * selection.rectangles)()
        
        for i in range(0, nr_rects):
            rects[i].r.left = rect_array[i].left
            rects[i].r.top = rect_array[i].top
            rects[i].r.width = rect_array[i].width
            rects[i].r.height = rect_array[i].height
            
        selection.pr = ctypes.cast(ctypes.pointer(rects), ctypes.POINTER(raw.v4l2_ext_rect))
        try:
            self._ioctl(IOC.S_SELECTION, selection)
        except OSError as error:
            #print(error.errno)
            raise

    def start(self):
        btype = raw.v4l2_buf_type(self.buffer_type)
        self._ioctl(IOC.STREAMON, btype)

    def stop(self):
        if not self.device.closed:
            btype = raw.v4l2_buf_type(self.buffer_type)
            self._ioctl(IOC.STREAMOFF, btype)


class BaseBuffer:
    def __init__(
        self, device, index=0, buffer_type=BufferType.VIDEO_CAPTURE, queue=True
    ):
        self._context_level = 0
        self.device = device
        self.index = index
        self.buffer_type = buffer_type
        self.queue = queue

    def _v4l2_buffer(self):
        buff = raw.v4l2_buffer()
        buff.index = self.index
        buff.type = self.buffer_type
        return buff

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request.value, arg=arg)

    def close(self):
        pass


class BufferMMAP(BaseBuffer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        buff = self._v4l2_buffer()
        self._ioctl(IOC.QUERYBUF, buff)
        self.mmap = mmap.mmap(self.device.fileno(), buff.length, offset=buff.m.offset)
        self.length = buff.length
        if self.queue:
            self._ioctl(IOC.QBUF, buff)

    def _v4l2_buffer(self):
        buff = super()._v4l2_buffer()
        buff.memory = Memory.MMAP
        return buff

    def close(self):
        if self.mmap is not None:
            self.mmap.close()
            self.mmap = None

    def raw_read(self, buff):
        result = self.mmap[: buff.bytesused]
        if self.queue:
            self._ioctl(IOC.QBUF, buff)
        return result

    def read(self, buff):
        select.select((self.device,), (), ())
        return self.raw_read(buff)


class Buffers:
    def __init__(
        self,
        device,
        buffer_type=BufferType.VIDEO_CAPTURE,
        buffer_size=1,
        buffer_queue=True,
        memory=Memory.MMAP,
    ):
        self._context_level = 0
        self.device = device
        self.buffer_size = buffer_size
        self.buffer_type = buffer_type
        self.buffer_queue = buffer_queue
        self.memory = memory
        self.buffers = self._create_buffers()

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def _ioctl(self, request, arg=0):
        return self.device._ioctl(request.value, arg=arg)

    def _create_buffers(self):
        if self.memory != Memory.MMAP:
            raise TypeError(f"Unsupported buffer type {self.memory.name!r}")
        r = raw.v4l2_requestbuffers()
        r.count = self.buffer_size
        r.type = self.buffer_type
        r.memory = self.memory
        self._ioctl(IOC.REQBUFS, r)
        if not r.count:
            raise IOError("Not enough buffer memory")
        return [
            BufferMMAP(self.device, index, self.buffer_type, self.buffer_queue)
            for index in range(r.count)
        ]

    def close(self):
        if self.buffers:
            for buff in self.buffers:
                buff.close()
            self.buffers = None

    def raw_read(self):
        buff = self.buffers[0]._v4l2_buffer()
        self._ioctl(IOC.DQBUF, buff)
        return self.buffers[buff.index].raw_read(buff)

    def read(self):
        select.select((self.device,), (), ())
        return self.raw_read()


class VideoStream:
    def __init__(
        self, video_capture, buffer_size=1, buffer_queue=True, memory=Memory.MMAP
    ):
        self._context_level = 0
        self.video_capture = video_capture
        self.buffers = Buffers(
            video_capture.device,
            video_capture.buffer_type,
            buffer_size,
            buffer_queue,
            memory,
        )

    def __enter__(self):
        self._context_level += 1
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._context_level -= 1
        if not self._context_level:
            self.close()

    def __iter__(self):
        return Stream(self)

    async def __aiter__(self):
        async for frame in AsyncStream(self):
            yield frame

    def close(self):
        self.buffers.close()

    def raw_read(self):
        return self.buffers.raw_read()

    def read(self):
        return self.buffers.read()


def Stream(stream):
    stream.video_capture.start()
    try:
        while True:
            yield stream.read()
    finally:
        stream.video_capture.stop()


async def AsyncStream(stream):
    import asyncio

    cap = stream.video_capture
    fd = cap.device.fileno()
    loop = asyncio.get_event_loop()
    event = asyncio.Event()
    loop.add_reader(fd, event.set)
    try:
        cap.start()
        while True:
            await event.wait()
            event.clear()
            yield stream.raw_read()
    finally:
        cap.stop()
        loop.remove_reader(fd)


def iter_video_files(path="/dev"):
    path = pathlib.Path(path)
    return path.glob("video*")


def iter_devices(path="/dev"):
    return (Device(name) for name in iter_video_files(path=path))


def iter_video_capture_devices(path="/dev"):
    def filt(filename):
        with fopen(filename) as fobj:
            caps = read_capabilities(fobj.fileno())
            return Capability.VIDEO_CAPTURE in Capability(caps.device_caps)

    return (Device(name) for name in filter(filt, iter_video_files(path)))
