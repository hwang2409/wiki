"""H.264 SPS/PPS RBSP parse + canonical re-encode.

Round 9 review closed the last byte-smuggling path in avcC: previously the
SPS/PPS NAL bodies were copied through opaquely once the outer length fields
validated. This module decodes each SPS/PPS RBSP down to its syntax
elements, validates the field values against the H.264 spec, and emits fresh
canonical bits from the decoded values. Any bit not consumed by the parser
causes rejection, and any mutation inside a declared NAL either changes a
parsed value (so the re-encoded output differs) or fails parsing.

Scope choices:
- Baseline through High/High10/High422/High444 profiles are decoded field-
  by-field, including scaling matrices, VUI parameters, and HRD parameters.
- SPS extension NALs (nal_unit_type 13) are rejected — they are used only
  for auxiliary picture streams (alpha layer), never seen on artifact
  uploads, so refusing them is safer than adding a partial parser.
- Reserved bit constraints from the spec are enforced (any nonconforming
  reserved bit rejects the file).
"""
from __future__ import annotations

import math

from .base import MediaScrubError


_H264_MAX_DIMENSION = 8192
_H264_MAX_PIXELS = 16_777_216
_H264_MAX_DPB_MBS = {
    9: 396, 10: 396, 11: 900, 12: 2376, 13: 2376, 20: 2376,
    21: 4752, 22: 8100, 30: 8100, 31: 18000, 32: 20480,
    40: 32768, 41: 32768, 42: 34816, 50: 110400, 51: 184320,
    52: 184320, 60: 696960, 61: 983040, 62: 2073600,
}
_H264_ASPECT_RATIOS = {
    1: (1, 1),
    2: (12, 11),
    3: (10, 11),
    4: (16, 11),
    5: (40, 33),
    6: (24, 11),
    7: (20, 11),
    8: (32, 11),
    9: (80, 33),
    10: (18, 11),
    11: (15, 11),
    12: (64, 33),
    13: (160, 99),
    14: (4, 3),
    15: (3, 2),
    16: (2, 1),
}


class _BitReader:
    __slots__ = ("_data", "_bit_pos", "_total_bits")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._bit_pos = 0
        self._total_bits = len(data) * 8

    def remaining(self) -> int:
        return self._total_bits - self._bit_pos

    def read_bits(self, n: int) -> int:
        if n < 0 or n > 32:
            raise MediaScrubError("h264 bit read width out of range")
        if self._bit_pos + n > self._total_bits:
            raise MediaScrubError("h264 bit read past end of RBSP")
        value = 0
        for _ in range(n):
            byte = self._data[self._bit_pos >> 3]
            bit = (byte >> (7 - (self._bit_pos & 7))) & 1
            value = (value << 1) | bit
            self._bit_pos += 1
        return value

    def read_u1(self) -> int:
        return self.read_bits(1)

    def read_ue(self) -> int:
        zeros = 0
        while zeros < 32:
            if self._bit_pos >= self._total_bits:
                raise MediaScrubError("h264 ue leading-zero run past end of RBSP")
            byte = self._data[self._bit_pos >> 3]
            bit = (byte >> (7 - (self._bit_pos & 7))) & 1
            self._bit_pos += 1
            if bit == 1:
                break
            zeros += 1
        else:
            raise MediaScrubError("h264 ue leading zeros exceed 32")
        if zeros == 0:
            return 0
        tail = self.read_bits(zeros)
        return (1 << zeros) - 1 + tail

    def read_se(self) -> int:
        code = self.read_ue()
        if code == 0:
            return 0
        if code & 1:
            return (code + 1) >> 1
        return -(code >> 1)

    def more_rbsp_data(self) -> bool:
        # Per H.264 7.2: more_rbsp_data() is false when only the stop bit
        # (single 1 followed by zeros to byte boundary) remains.
        if self._bit_pos >= self._total_bits:
            return False
        remaining = self._total_bits - self._bit_pos
        # Look ahead: find the last 1-bit in the buffer.
        # If the current bit is the stop bit followed by zeros to end, return False.
        # Scan from current position to end.
        found_one = False
        for i in range(self._bit_pos, self._total_bits):
            byte = self._data[i >> 3]
            bit = (byte >> (7 - (i & 7))) & 1
            if bit == 1:
                if found_one:
                    return True
                found_one = True
        return not found_one and remaining > 0

    def read_rbsp_trailing_bits(self) -> None:
        stop = self.read_bits(1)
        if stop != 1:
            raise MediaScrubError("h264 rbsp trailing bits missing stop-one bit")
        # Zero-pad to byte boundary.
        while self._bit_pos & 7:
            if self.read_bits(1) != 0:
                raise MediaScrubError("h264 rbsp trailing bits non-zero pad")
        if self._bit_pos != self._total_bits:
            raise MediaScrubError("h264 rbsp has bits past trailing byte boundary")


class _BitWriter:
    __slots__ = ("_out", "_bit_pos")

    def __init__(self) -> None:
        self._out = bytearray()
        self._bit_pos = 0

    def write_bits(self, value: int, n: int) -> None:
        if n < 0 or n > 32:
            raise MediaScrubError("h264 bit write width out of range")
        if value < 0 or value >= (1 << n):
            raise MediaScrubError("h264 bit write value out of range")
        for i in range(n - 1, -1, -1):
            bit = (value >> i) & 1
            if self._bit_pos & 7 == 0:
                self._out.append(0)
            byte_idx = self._bit_pos >> 3
            self._out[byte_idx] |= bit << (7 - (self._bit_pos & 7))
            self._bit_pos += 1

    def write_u1(self, value: int) -> None:
        self.write_bits(value & 1, 1)

    def write_ue(self, value: int) -> None:
        if value < 0:
            raise MediaScrubError("h264 ue write negative value")
        m = value + 1
        n = m.bit_length() - 1
        if n > 32:
            raise MediaScrubError("h264 ue write value too large")
        # n leading zeros, then the (n+1)-bit binary of m.
        self.write_bits(0, n)
        self.write_bits(m, n + 1)

    def write_se(self, value: int) -> None:
        if value <= 0:
            code = -2 * value
        else:
            code = 2 * value - 1
        self.write_ue(code)

    def write_rbsp_trailing_bits(self) -> None:
        self.write_bits(1, 1)
        while self._bit_pos & 7:
            self.write_bits(0, 1)

    def to_bytes(self) -> bytes:
        return bytes(self._out)


def _rbsp_unescape(nal_body: bytes) -> bytes:
    """Remove emulation prevention bytes (0x00 0x00 0x03) inside a NAL body."""
    out = bytearray()
    zeros = 0
    i = 0
    n = len(nal_body)
    while i < n:
        b = nal_body[i]
        if zeros >= 2 and b == 0x03:
            # Emulation prevention byte. The next byte must be <= 0x03
            # per H.264 7.4.1.1; otherwise this was not a real 03 escape.
            if i + 1 >= n:
                raise MediaScrubError("h264 emulation-prevention 03 at end of NAL")
            follower = nal_body[i + 1]
            if follower > 0x03:
                raise MediaScrubError("h264 emulation-prevention 03 followed by illegal byte")
            zeros = 0
            i += 1
            continue
        out.append(b)
        if b == 0:
            zeros += 1
        else:
            zeros = 0
        i += 1
    return bytes(out)


def _rbsp_escape(rbsp: bytes) -> bytes:
    """Insert emulation prevention bytes to convert RBSP to NAL byte stream."""
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 0x03:
            out.append(0x03)
            zeros = 0
        out.append(b)
        if b == 0:
            zeros += 1
        else:
            zeros = 0
    # H.264 7.4.1.1 requires trailing zero bytes get an escape too, but a
    # valid trailing_bits stop-one guarantees the last RBSP byte is nonzero.
    return bytes(out)


_HIGH_PROFILES = frozenset({
    44, 83, 86, 100, 110, 118, 122, 128, 134, 135, 138, 139, 144, 244,
})
_SUPPORTED_PROFILES = frozenset({66, 77}) | _HIGH_PROFILES


def _copy_scaling_list(reader: _BitReader, writer: _BitWriter, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            delta_scale = reader.read_se()
            writer.write_se(delta_scale)
            next_scale = (last_scale + delta_scale + 256) % 256
        if next_scale != 0:
            last_scale = next_scale


def _copy_hrd_parameters(reader: _BitReader, writer: _BitWriter) -> None:
    cpb_cnt_minus1 = reader.read_ue()
    if cpb_cnt_minus1 > 31:
        raise MediaScrubError("h264 hrd cpb_cnt_minus1 out of range")
    writer.write_ue(cpb_cnt_minus1)
    writer.write_bits(reader.read_bits(4), 4)  # bit_rate_scale
    writer.write_bits(reader.read_bits(4), 4)  # cpb_size_scale
    for _ in range(cpb_cnt_minus1 + 1):
        writer.write_ue(reader.read_ue())      # bit_rate_value_minus1
        writer.write_ue(reader.read_ue())      # cpb_size_value_minus1
        writer.write_u1(reader.read_u1())      # cbr_flag
    writer.write_bits(reader.read_bits(5), 5)  # initial_cpb_removal_delay_length_minus1
    writer.write_bits(reader.read_bits(5), 5)  # cpb_removal_delay_length_minus1
    writer.write_bits(reader.read_bits(5), 5)  # dpb_output_delay_length_minus1
    writer.write_bits(reader.read_bits(5), 5)  # time_offset_length


def _copy_vui_parameters(
    reader: _BitReader, writer: _BitWriter, max_dpb_frames: int,
) -> tuple[int, int] | None:
    aspect_ratio_info_present_flag = reader.read_u1()
    writer.write_u1(aspect_ratio_info_present_flag)
    sar: tuple[int, int] | None = None
    if aspect_ratio_info_present_flag:
        aspect_ratio_idc = reader.read_bits(8)
        if aspect_ratio_idc == 0:
            sar = None
        elif aspect_ratio_idc == 255:
            sar_width = reader.read_bits(16)
            sar_height = reader.read_bits(16)
            if sar_width == 0 or sar_height == 0:
                raise MediaScrubError("h264 VUI extended SAR must be positive")
            divisor = math.gcd(sar_width, sar_height)
            sar = (sar_width // divisor, sar_height // divisor)
        else:
            sar = _H264_ASPECT_RATIOS.get(aspect_ratio_idc)
            if sar is None:
                raise MediaScrubError(
                    f"h264 VUI aspect_ratio_idc {aspect_ratio_idc} is unsupported"
                )
        writer.write_bits(aspect_ratio_idc, 8)
        if aspect_ratio_idc == 255:
            assert sar is not None
            writer.write_bits(sar[0], 16)
            writer.write_bits(sar[1], 16)

    overscan_info_present_flag = reader.read_u1()
    writer.write_u1(overscan_info_present_flag)
    if overscan_info_present_flag:
        writer.write_u1(reader.read_u1())  # overscan_appropriate_flag

    video_signal_type_present_flag = reader.read_u1()
    writer.write_u1(video_signal_type_present_flag)
    if video_signal_type_present_flag:
        video_format = reader.read_bits(3)
        if video_format > 5:
            raise MediaScrubError("h264 VUI video_format out of range")
        writer.write_bits(video_format, 3)
        writer.write_u1(reader.read_u1())          # video_full_range_flag
        colour_description_present_flag = reader.read_u1()
        writer.write_u1(colour_description_present_flag)
        if colour_description_present_flag:
            writer.write_bits(reader.read_bits(8), 8)  # colour_primaries
            writer.write_bits(reader.read_bits(8), 8)  # transfer_characteristics
            writer.write_bits(reader.read_bits(8), 8)  # matrix_coefficients

    chroma_loc_info_present_flag = reader.read_u1()
    writer.write_u1(chroma_loc_info_present_flag)
    if chroma_loc_info_present_flag:
        chroma_top = reader.read_ue()
        chroma_bottom = reader.read_ue()
        if chroma_top > 5 or chroma_bottom > 5:
            raise MediaScrubError("h264 VUI chroma location out of range")
        writer.write_ue(chroma_top)
        writer.write_ue(chroma_bottom)

    timing_info_present_flag = reader.read_u1()
    writer.write_u1(timing_info_present_flag)
    if timing_info_present_flag:
        num_units_in_tick = reader.read_bits(32)
        time_scale = reader.read_bits(32)
        if num_units_in_tick == 0 or time_scale == 0:
            raise MediaScrubError("h264 VUI timing values must be positive")
        writer.write_bits(num_units_in_tick, 32)
        writer.write_bits(time_scale, 32)
        writer.write_u1(reader.read_u1())            # fixed_frame_rate_flag

    nal_hrd_present = reader.read_u1()
    writer.write_u1(nal_hrd_present)
    if nal_hrd_present:
        _copy_hrd_parameters(reader, writer)

    vcl_hrd_present = reader.read_u1()
    writer.write_u1(vcl_hrd_present)
    if vcl_hrd_present:
        _copy_hrd_parameters(reader, writer)

    if nal_hrd_present or vcl_hrd_present:
        writer.write_u1(reader.read_u1())  # low_delay_hrd_flag

    writer.write_u1(reader.read_u1())  # pic_struct_present_flag

    bitstream_restriction_flag = reader.read_u1()
    writer.write_u1(bitstream_restriction_flag)
    if bitstream_restriction_flag:
        writer.write_u1(reader.read_u1())  # motion_vectors_over_pic_boundaries_flag
        max_bytes_per_pic_denom = reader.read_ue()
        max_bits_per_mb_denom = reader.read_ue()
        max_mv_horizontal = reader.read_ue()
        max_mv_vertical = reader.read_ue()
        if any(
            value > 16
            for value in (
                max_bytes_per_pic_denom,
                max_bits_per_mb_denom,
                max_mv_horizontal,
                max_mv_vertical,
            )
        ):
            raise MediaScrubError("h264 VUI bitstream restriction value out of range")
        writer.write_ue(max_bytes_per_pic_denom)
        writer.write_ue(max_bits_per_mb_denom)
        writer.write_ue(max_mv_horizontal)
        writer.write_ue(max_mv_vertical)
        max_num_reorder_frames = reader.read_ue()
        max_dec_frame_buffering = reader.read_ue()
        if (
            max_num_reorder_frames > max_dec_frame_buffering
            or max_dec_frame_buffering > max_dpb_frames
        ):
            raise MediaScrubError("h264 VUI decoded-picture-buffer limits are invalid")
        writer.write_ue(max_num_reorder_frames)
        writer.write_ue(max_dec_frame_buffering)
    return sar


def _parse_and_emit_sps_rbsp(
    rbsp: bytes,
) -> tuple[bytes, int, tuple[int, int], tuple[int, int] | None]:
    reader = _BitReader(rbsp)
    writer = _BitWriter()

    profile_idc = reader.read_bits(8)
    if profile_idc not in _SUPPORTED_PROFILES:
        raise MediaScrubError(
            f"h264 SPS profile_idc {profile_idc} is outside the supported subset"
        )
    writer.write_bits(profile_idc, 8)
    # 6 constraint_setN_flag bits then 2 reserved_zero bits
    constraint_bits = reader.read_bits(8)
    if constraint_bits & 0x03 != 0:
        raise MediaScrubError("h264 sps reserved_zero_2bits nonzero")
    writer.write_bits(constraint_bits, 8)
    level_idc = reader.read_bits(8)
    writer.write_bits(level_idc, 8)

    sps_id = reader.read_ue()
    if sps_id > 31:
        raise MediaScrubError("h264 sps seq_parameter_set_id out of range")
    writer.write_ue(sps_id)

    chroma_format_idc = 1
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = reader.read_ue()
        if chroma_format_idc > 3:
            raise MediaScrubError("h264 sps chroma_format_idc out of range")
        writer.write_ue(chroma_format_idc)
        if chroma_format_idc == 3:
            writer.write_u1(reader.read_u1())  # separate_colour_plane_flag
        bd_luma = reader.read_ue()
        bd_chroma = reader.read_ue()
        if bd_luma > 6 or bd_chroma > 6:
            raise MediaScrubError("h264 sps bit_depth out of range")
        writer.write_ue(bd_luma)
        writer.write_ue(bd_chroma)
        writer.write_u1(reader.read_u1())  # qpprime_y_zero_transform_bypass_flag
        seq_scaling_matrix_present = reader.read_u1()
        writer.write_u1(seq_scaling_matrix_present)
        if seq_scaling_matrix_present:
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                seq_scaling_list_present = reader.read_u1()
                writer.write_u1(seq_scaling_list_present)
                if seq_scaling_list_present:
                    size = 16 if i < 6 else 64
                    _copy_scaling_list(reader, writer, size)

    log2_max_frame_num_minus4 = reader.read_ue()
    if log2_max_frame_num_minus4 > 12:
        raise MediaScrubError("h264 sps log2_max_frame_num_minus4 out of range")
    writer.write_ue(log2_max_frame_num_minus4)

    pic_order_cnt_type = reader.read_ue()
    if pic_order_cnt_type > 2:
        raise MediaScrubError("h264 sps pic_order_cnt_type out of range")
    writer.write_ue(pic_order_cnt_type)
    if pic_order_cnt_type == 0:
        log2_max_pic_order_cnt_lsb_minus4 = reader.read_ue()
        if log2_max_pic_order_cnt_lsb_minus4 > 12:
            raise MediaScrubError(
                "h264 sps log2_max_pic_order_cnt_lsb_minus4 out of range"
            )
        writer.write_ue(log2_max_pic_order_cnt_lsb_minus4)
    elif pic_order_cnt_type == 1:
        writer.write_u1(reader.read_u1())  # delta_pic_order_always_zero_flag
        writer.write_se(reader.read_se())  # offset_for_non_ref_pic
        writer.write_se(reader.read_se())  # offset_for_top_to_bottom_field
        num_ref_in_cycle = reader.read_ue()
        if num_ref_in_cycle > 255:
            raise MediaScrubError("h264 sps num_ref_frames_in_pic_order_cnt_cycle too large")
        writer.write_ue(num_ref_in_cycle)
        for _ in range(num_ref_in_cycle):
            writer.write_se(reader.read_se())

    max_num_ref_frames = reader.read_ue()
    writer.write_ue(max_num_ref_frames)
    writer.write_u1(reader.read_u1())  # gaps_in_frame_num_value_allowed_flag
    pic_width_in_mbs_minus1 = reader.read_ue()
    pic_height_in_map_units_minus1 = reader.read_ue()
    coded_width = (pic_width_in_mbs_minus1 + 1) * 16
    coded_height_multiplier = 2
    coded_height = (pic_height_in_map_units_minus1 + 1) * 16 * coded_height_multiplier
    writer.write_ue(pic_width_in_mbs_minus1)
    writer.write_ue(pic_height_in_map_units_minus1)
    frame_mbs_only_flag = reader.read_u1()
    if frame_mbs_only_flag:
        coded_height_multiplier = 1
        coded_height = (pic_height_in_map_units_minus1 + 1) * 16
    if (
        coded_width > _H264_MAX_DIMENSION
        or coded_height > _H264_MAX_DIMENSION
        or coded_width * coded_height > _H264_MAX_PIXELS
    ):
        raise MediaScrubError("h264 SPS coded dimensions exceed scrubber limits")
    max_dpb_mbs = _H264_MAX_DPB_MBS.get(level_idc)
    if max_dpb_mbs is None:
        raise MediaScrubError("h264 SPS level_idc has no DPB limit")
    frame_mbs = ((pic_width_in_mbs_minus1 + 1)
                 * (pic_height_in_map_units_minus1 + 1)
                 * coded_height_multiplier)
    max_dpb_frames = min(16, max_dpb_mbs // frame_mbs)
    if max_dpb_frames < 1 or max_num_ref_frames > max_dpb_frames:
        raise MediaScrubError("h264 SPS max_num_ref_frames exceeds level DPB limit")
    writer.write_u1(frame_mbs_only_flag)
    if not frame_mbs_only_flag:
        writer.write_u1(reader.read_u1())  # mb_adaptive_frame_field_flag
    writer.write_u1(reader.read_u1())  # direct_8x8_inference_flag
    frame_cropping_flag = reader.read_u1()
    crop_left = crop_right = crop_top = crop_bottom = 0
    writer.write_u1(frame_cropping_flag)
    if frame_cropping_flag:
        crop_left = reader.read_ue()
        crop_right = reader.read_ue()
        crop_top = reader.read_ue()
        crop_bottom = reader.read_ue()
        writer.write_ue(crop_left)
        writer.write_ue(crop_right)
        writer.write_ue(crop_top)
        writer.write_ue(crop_bottom)

    if chroma_format_idc == 0:
        crop_unit_x = 1
        crop_unit_y = coded_height_multiplier
    elif chroma_format_idc == 1:
        crop_unit_x = 2
        crop_unit_y = 2 * coded_height_multiplier
    elif chroma_format_idc == 2:
        crop_unit_x = 2
        crop_unit_y = coded_height_multiplier
    else:
        crop_unit_x = 1
        crop_unit_y = coded_height_multiplier
    display_width = coded_width - crop_unit_x * (crop_left + crop_right)
    display_height = coded_height - crop_unit_y * (crop_top + crop_bottom)
    if (
        display_width <= 0
        or display_height <= 0
        or display_width > _H264_MAX_DIMENSION
        or display_height > _H264_MAX_DIMENSION
        or display_width * display_height > _H264_MAX_PIXELS
    ):
        raise MediaScrubError("h264 SPS cropped dimensions are invalid")

    vui_present = reader.read_u1()
    writer.write_u1(vui_present)
    vui_sar: tuple[int, int] | None = None
    if vui_present:
        vui_sar = _copy_vui_parameters(reader, writer, max_dpb_frames)

    reader.read_rbsp_trailing_bits()
    writer.write_rbsp_trailing_bits()
    return writer.to_bytes(), sps_id, (display_width, display_height), vui_sar


def canonicalise_sps_with_dimensions(
    nal_bytes: bytes,
) -> tuple[bytes, int, tuple[int, int], tuple[int, int] | None]:
    """Canonicalise an SPS and return dimensions plus its VUI SAR."""
    _validate_nal_header(nal_bytes, 7, require_nonzero_ref=True)
    header = nal_bytes[0]
    rbsp = _rbsp_unescape(nal_bytes[1:])
    if not rbsp:
        raise MediaScrubError("h264 SPS RBSP is empty after unescape")
    new_rbsp, sps_id, dimensions, vui_sar = _parse_and_emit_sps_rbsp(rbsp)
    nal_ref_idc = (header >> 5) & 0x3
    return (
        bytes([(nal_ref_idc << 5) | 7]) + _rbsp_escape(new_rbsp),
        sps_id,
        dimensions,
        vui_sar,
    )


def _parse_and_emit_pps_rbsp(rbsp: bytes) -> tuple[bytes, int, int]:
    reader = _BitReader(rbsp)
    writer = _BitWriter()

    pps_id = reader.read_ue()
    sps_id = reader.read_ue()
    if pps_id > 255 or sps_id > 31:
        raise MediaScrubError("h264 pps parameter_set_id out of range")
    writer.write_ue(pps_id)
    writer.write_ue(sps_id)

    entropy_coding_mode_flag = reader.read_u1()
    writer.write_u1(entropy_coding_mode_flag)
    writer.write_u1(reader.read_u1())  # bottom_field_pic_order_in_frame_present_flag

    num_slice_groups_minus1 = reader.read_ue()
    if num_slice_groups_minus1 > 7:
        raise MediaScrubError("h264 pps num_slice_groups_minus1 out of range")
    writer.write_ue(num_slice_groups_minus1)
    if num_slice_groups_minus1 > 0:
        raise MediaScrubError(
            "h264 PPS FMO slice groups are outside the supported subset"
        )

    num_ref_idx_l0_default_active_minus1 = reader.read_ue()
    num_ref_idx_l1_default_active_minus1 = reader.read_ue()
    if (
        num_ref_idx_l0_default_active_minus1 > 31
        or num_ref_idx_l1_default_active_minus1 > 31
    ):
        raise MediaScrubError("h264 pps default reference count out of range")
    writer.write_ue(num_ref_idx_l0_default_active_minus1)
    writer.write_ue(num_ref_idx_l1_default_active_minus1)
    writer.write_u1(reader.read_u1())  # weighted_pred_flag
    weighted_bipred_idc = reader.read_bits(2)
    if weighted_bipred_idc > 2:
        raise MediaScrubError("h264 pps weighted_bipred_idc out of range")
    writer.write_bits(weighted_bipred_idc, 2)
    pic_init_qp_minus26 = reader.read_se()
    pic_init_qs_minus26 = reader.read_se()
    chroma_qp_index_offset = reader.read_se()
    if not -26 <= pic_init_qp_minus26 <= 25:
        raise MediaScrubError("h264 pps pic_init_qp_minus26 out of range")
    if not -26 <= pic_init_qs_minus26 <= 25:
        raise MediaScrubError("h264 pps pic_init_qs_minus26 out of range")
    if not -12 <= chroma_qp_index_offset <= 12:
        raise MediaScrubError("h264 pps chroma_qp_index_offset out of range")
    writer.write_se(pic_init_qp_minus26)
    writer.write_se(pic_init_qs_minus26)
    writer.write_se(chroma_qp_index_offset)
    writer.write_u1(reader.read_u1())  # deblocking_filter_control_present_flag
    writer.write_u1(reader.read_u1())  # constrained_intra_pred_flag
    writer.write_u1(reader.read_u1())  # redundant_pic_cnt_present_flag

    if reader.more_rbsp_data():
        writer.write_u1(reader.read_u1())  # transform_8x8_mode_flag
        pic_scaling_matrix_present_flag = reader.read_u1()
        writer.write_u1(pic_scaling_matrix_present_flag)
        if pic_scaling_matrix_present_flag:
            # The list count depends on the SPS chroma_format_idc and the
            # PPS transform_8x8_mode_flag. Standard: for i < 6 + (chroma!=3 ? 2 : 6) * transform_8x8.
            # Without the SPS context here we accept up to 12 lists and stop
            # once more_rbsp_data() is false — but the spec fixes the count.
            # We conservatively reject scaling matrices without SPS context:
            # every real encoder that emits one also carries seq_scaling_matrix.
            raise MediaScrubError(
                "h264 pps carries scaling matrix without SPS context; rejected"
            )
        second_chroma_qp_index_offset = reader.read_se()
        if not -12 <= second_chroma_qp_index_offset <= 12:
            raise MediaScrubError(
                "h264 pps second_chroma_qp_index_offset out of range"
            )
        writer.write_se(second_chroma_qp_index_offset)

    reader.read_rbsp_trailing_bits()
    writer.write_rbsp_trailing_bits()
    return writer.to_bytes(), pps_id, sps_id


def canonicalise_nal_with_ids(
    nal_bytes: bytes, expected_nal_type: int,
) -> tuple[bytes, int, int | None]:
    """Canonicalise an SPS or PPS and return its parameter-set identifiers."""
    nal_ref_idc = _validate_nal_header(
        nal_bytes, expected_nal_type, require_nonzero_ref=True,
    )
    header = nal_bytes[0]
    nal_type = expected_nal_type
    rbsp = _rbsp_unescape(nal_bytes[1:])
    if not rbsp:
        raise MediaScrubError("h264 RBSP is empty after unescape")

    if expected_nal_type == 7:
        new_rbsp, sps_id, _dimensions, _vui_sar = _parse_and_emit_sps_rbsp(rbsp)
        return (
            bytes([(nal_ref_idc << 5) | nal_type]) + _rbsp_escape(new_rbsp),
            sps_id,
            None,
        )
    if expected_nal_type == 8:
        new_rbsp, pps_id, sps_id = _parse_and_emit_pps_rbsp(rbsp)
        return (
            bytes([(nal_ref_idc << 5) | nal_type]) + _rbsp_escape(new_rbsp),
            pps_id,
            sps_id,
        )
    raise MediaScrubError(f"h264 canonicalise: unsupported NAL type {expected_nal_type}")


def _validate_nal_header(
    nal_bytes: bytes,
    expected_nal_type: int,
    *,
    require_nonzero_ref: bool = False,
    require_zero_ref: bool = False,
) -> int:
    if len(nal_bytes) < 1:
        raise MediaScrubError("h264 NAL too short for header")
    header = nal_bytes[0]
    if header & 0x80:
        raise MediaScrubError("h264 NAL forbidden_zero_bit set")
    nal_ref_idc = (header >> 5) & 0x3
    nal_type = header & 0x1F
    if nal_type != expected_nal_type:
        raise MediaScrubError(
            f"h264 NAL type {nal_type} does not match expected {expected_nal_type}"
        )
    if require_nonzero_ref and nal_ref_idc == 0:
        raise MediaScrubError(
            f"h264 NAL type {expected_nal_type} has zero nal_ref_idc"
        )
    if require_zero_ref and nal_ref_idc != 0:
        raise MediaScrubError(
            f"h264 NAL type {expected_nal_type} has non-zero nal_ref_idc"
        )
    return nal_ref_idc


def canonicalise_nal(nal_bytes: bytes, expected_nal_type: int) -> bytes:
    """Parse a NAL, validate its type, decode + re-encode the RBSP, return
    the canonical NAL byte stream (header byte + escaped RBSP).
    """
    canonical, _first_id, _second_id = canonicalise_nal_with_ids(
        nal_bytes, expected_nal_type,
    )
    return canonical


def parse_slice_pps_id(nal_bytes: bytes) -> int:
    """Read the PPS identifier from a coded-slice NAL header and RBSP."""
    if not nal_bytes:
        raise MediaScrubError("h264 slice NAL is empty")
    nal_type = nal_bytes[0] & 0x1F
    if nal_type not in (1, 5):
        raise MediaScrubError("h264 NAL is not a coded slice")
    rbsp = _rbsp_unescape(nal_bytes[1:])
    reader = _BitReader(rbsp)
    reader.read_ue()  # first_mb_in_slice
    reader.read_ue()  # slice_type
    return reader.read_ue()  # pic_parameter_set_id
