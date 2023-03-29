#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# render_drawfile.py
#
# The Python script in this file is part of drawfile-render: a tool for
# rendering Acorn !Draw files to PNG and SVG.
#
# Copyright (C) 2010-2023 Dominic Ford <https://dcford.org.uk/>
#
# This code is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# You should have received a copy of the GNU General Public License along with
# this file; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA  02110-1301, USA

# ----------------------------------------------------------------------------

"""
This Python script renders Acorn Draw files in a variety of formats

References:
    http://justsolve.archiveteam.org/wiki/Acorn_Draw
    https://www.riscosopen.org/wiki/documentation/show/File%20formats:%20DrawFile
    http://www.riscos.com/support/users/grapharm/chap17.htm
    http://www.wss.co.uk/pinknoise/Docs/Arc/Draw/DrawFiles.html
"""

import argparse
import io
import glob
import logging
import os
import sys

from typing import Dict, List, Optional, Sequence

from graphics_context import GraphicsContext, GraphicsPage
import spritefile, spr2img
import temporary_directory


def bytes_to_uint(size: int, byte_array: bytes, position: int) -> int:
    """
    Convert an array of bytes into an unsigned integer of arbitrary byte width.

    :param size:
        The number of bytes in the unsigned integer.
    :param byte_array:
        The input array of bytes.
    :param position:
        The position of the start of the integer
    :return:
        Integer value
    """
    out = 0
    for index in range(size):
        out = out | (byte_array[position + index] << (index * 8))
    return out


def colour_dict_from_int(uint: int) -> dict:
    """
    Fetch an RGB colour from a 32-bit int.

    :param uint:
        Integer colour specification
    :return:
        Dictionary describing the colour
    """

    return {
        "r": (uint & 0x0000FF00) >> 8,
        "g": (uint & 0x00FF0000) >> 16,
        "b": (uint & 0xFF000000) >> 24,
        "transparent": (uint & 0x000000FF) == 0xFF,
    }


def context_colour_from_int(uint: int) -> Sequence[float]:
    """
    Fetch a Cairo RGBA colour from a 32-bit int.

    :param uint:
        Integer colour specification
    :return:
        Sequence of RGBA components, in the range 0-1
    """

    colour_dict: dict = colour_dict_from_int(uint=uint)

    return [colour_dict["r"] / 255., colour_dict["g"] / 255., colour_dict["b"] / 255.,
            0. if colour_dict["transparent"] else 1.]


class DrawFileRender:
    # Draw files measure positions in units of 1/(180*256) inches
    pixel: float = 5.51215278e-07  # metres

    # Margin to allow around the image area / metres
    margin: float = 0.005

    # Draw file object types, as documented in the RISC OS manual
    object_types: Dict[int, dict] = {
        0: {
            "name": "Font table",
            "bbox": False,
            "bbox_include_in_render": False
        },
        1: {
            "name": "Text object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "text_colour": [0, "uint", 4],
                "bg_colour_hint": [4, "uint", 4],
                "text_style": [8, "uint", 4],
                "x_size": [12, "uint", 4],
                "y_size": [16, "uint", 4],
                "x_baseline": [20, "uint", 4],
                "y_baseline": [24, "uint", 4],
                "text": [28, "str", 0]
            }
        },
        2: {
            "name": "Path object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "fill_colour": [0, "uint", 4],
                "outline_colour": [4, "uint", 4],
                "outline_width": [8, "uint", 4],
                "path_style": [12, "uint", 4]
            }
        },
        5: {
            "name": "Sprite object",
            "bbox": True,
            "bbox_include_in_render": True
        },
        6: {
            "name": "Group object",
            "bbox": True,
            "bbox_include_in_render": False,
            "fields": {
                "name": [0, "str", 12]
            },
            "children_start": 12
        },
        7: {
            "name": "Tagged object",
            "bbox": True,
            "bbox_include_in_render": False,
            "fields": {
                "tag_id": [0, "uint", 4]
            },
            "children_start": 4
        },
        9: {
            "name": "Text area object",
            "bbox": True,
            "bbox_include_in_render": True,
            "children_start": 0,
            "fields": {
                "zero": [0, "uint", 4],
                "reserved_1": [4, "uint", 4],
                "reserved_2": [8, "uint", 8],
                "colour_foreground": [12, "uint", 4],
                "colour_background": [16, "uint", 4],
                "text": [20, "str", 0]
            }
        },
        10: {
            "name": "Text column object",
            "bbox": True,
            "bbox_include_in_render": True
        },
        11: {
            "name": "Options object",
            "bbox": True,
            "bbox_include_in_render": False,
            "fields": {
                "paper_size": [0, "uint", 4],
                "paper_limits": [4, "uint", 4],
                "grid_spacing": [8, "uint", 8],  # double-precision floating point
                "grid_division": [16, "uint", 4],
                "grid_type": [20, "uint", 4],
                "grid_auto_adjust": [24, "uint", 4],
                "grid_visible": [28, "uint", 4],
                "grid_units": [32, "uint", 4],
                "zoom_multiplier": [36, "uint", 4],
                "zoom_divider": [40, "uint", 4],
                "zoom_locking": [44, "uint", 4],
                "toolbox_presence": [48, "uint", 4],
                "initial_entry_mode": [52, "uint", 4],
                "undo_buffer_size": [56, "uint", 4]
            },
        },
        12: {
            "name": "Transformed text object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "transformation_a": [0, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_b": [4, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_c": [8, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_d": [12, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_e": [16, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_f": [20, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "font_flags": [24, "uint", 4],
                "text_colour": [28, "uint", 4],
                "bg_colour_hint": [32, "uint", 4],
                "text_style": [36, "uint", 4],
                "x_size": [40, "uint", 4],
                "y_size": [44, "uint", 4],
                "x_baseline": [48, "uint", 4],
                "y_baseline": [52, "uint", 4],
                "text": [56, "str", 0]
            },
        },
        13: {
            "name": "Transformed sprite object",
            "bbox": True,
            "bbox_include_in_render": True,
            "fields": {
                "transformation_a": [0, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_b": [4, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_c": [8, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_d": [12, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_e": [16, "int/65536", 4],  # fixed-point number &XXXX.XXXX
                "transformation_f": [20, "int/65536", 4],  # fixed-point number &XXXX.XXXX
            },
        },
        16: {
            "name": "JPEG object",
            "bbox": True,
            "bbox_include_in_render": True
        },
        0x65: {
            "name": "[DrawPlus extension] DrawPlus settings",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x66: {
            "name": "[Vector extension] Static replicate",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x67: {
            "name": "[Vector extension] Dynamic replicate",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x69: {
            "name": "[Vector extension] Masked object",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x6A: {
            "name": "[Vector extension] Radiated object",
            "bbox": False,
            "bbox_include_in_render": False
        },
        0x6B: {
            "name": "[Vector extension] Skeleton for replications",
            "bbox": False,
            "bbox_include_in_render": False
        },
    }

    def __init__(self, filename: str):
        self.filename: str = filename

        # Read Drawfile into an array of bytes
        with open(filename, "rb") as file:
            self.bytes = file.read()

        # Read header of Drawfile
        self.size: int = len(self.bytes)
        self.draw_id: str = self.bytes[0:4].decode(encoding='iso-8859-1', errors='ignore')
        self.major_version: int = bytes_to_uint(size=4, byte_array=self.bytes, position=4)
        self.minor_version: int = bytes_to_uint(size=4, byte_array=self.bytes, position=8)
        self.generator: str = self.bytes[12:24].decode(encoding='iso-8859-1', errors='ignore')

        # Read bounding box
        # We will expand the limits above if we find objects outside the visible area, so keep a record of what
        # the header said
        self.x_min_as_read: int = bytes_to_uint(size=4, byte_array=self.bytes, position=24)
        self.y_min_as_read: int = bytes_to_uint(size=4, byte_array=self.bytes, position=28)
        self.x_max_as_read: int = bytes_to_uint(size=4, byte_array=self.bytes, position=32)
        self.y_max_as_read: int = bytes_to_uint(size=4, byte_array=self.bytes, position=36)

        # We will expand the limits above if we find objects outside the visible area, so keep a record of what
        # the header said
        self.x_min: int = self.x_min_as_read
        self.x_max: int = self.x_max_as_read
        self.y_min: int = self.y_min_as_read
        self.y_max: int = self.y_max_as_read
        self.factor_into_bbox(x=self.x_min, y=self.y_min)
        self.factor_into_bbox(x=self.x_max, y=self.y_max)

        # Read the objects that follow the header
        self.objects = []
        self.fetch_objects(target=self.objects, position=40, end_position=self.size)

        # If the bounding box is ridiculously large, sanitise it
        max_size = 5  # metres
        if (self.x_max - self.x_min) * self.pixel > max_size or (self.y_max - self.y_min) * self.pixel > max_size:
            logging.info("Removing ridiculously large bounding box")
            self.x_min = self.x_min_as_read - int(0.3 / self.pixel)
            self.x_max = self.x_max_as_read + int(0.3 / self.pixel)
            self.y_min = self.y_min_as_read - int(0.3 / self.pixel)
            self.y_max = self.y_max_as_read + int(0.3 / self.pixel)

    def factor_into_bbox(self, x: float, y: float) -> None:
        """
        Factor a point into the bounding box for this Drawfile
        :param x:
            Position, Drawfile pixels
        :param y:
            Position, Drawfile pixels
        """

        self.x_min = min(self.x_min, int(x - self.margin / self.pixel))
        self.x_max = max(self.x_max, int(x + self.margin / self.pixel))
        self.y_min = min(self.y_min, int(y - self.margin / self.pixel))
        self.y_max = max(self.y_max, int(y + self.margin / self.pixel))

    def x_pos(self, x: float) -> float:
        """
        Convert Drawfile coordinates into page coordinates (metres)
        :param x:
            Position, Drawfile pixels
        :return:
            Position, metres
        """
        return (x - self.x_min) * self.pixel

    def y_pos(self, y: float) -> float:
        """
        Convert Drawfile coordinates into page coordinates (metres)
        :param y:
            Position, Drawfile pixels
        :return:
            Position, metres
        """
        return (self.y_max - y) * self.pixel

    def fetch_objects(self, target: list, position: int, end_position: int, exit_on_zero: bool = False) -> None:
        """
        Read the list of objects contained within a Drawfile, or within a parent object.

        :param target:
            The list into which we insert the objects we extract.
        :param position:
            The position of the start of the list of objects within the input file
        :param end_position:
            The maximum position in the file beyond which we should not read
        :param exit_on_zero:
            Exit if an object begins with a null word
        :return:
            None
        """
        while position < end_position:
            new_object = self.fetch_object(position=position, exit_on_zero=exit_on_zero)
            if new_object is None:
                break
            target.append(new_object)
            position += new_object["size"]

            # Impose a minimum size on an object, as otherwise infinite recursion is possible
            if new_object["size"] < 8:
                logging.info("Drawfile object with illegal size of {:d} bytes".format(new_object["size"]))
                break

    def fetch_object(self, position: int, exit_on_zero: bool = False) -> Optional[Dict]:
        """
        Fetch a single object from the Drawfile.

        :param position:
            The position of the start of this object
        :param exit_on_zero:
            Exit if an object begins with a null word
        :return:
            A dictionary describing the object we extracted
        """
        # Read the object header
        type_id_32: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position)

        # Draw Plus stores other flags in most significant 24 bits, so ignore these in determining object type
        type_id: int = type_id_32 & 0xFF

        # A zero indicates the end of the string of objects
        if type_id == 0 and exit_on_zero:
            return None

        # Create dictionary describing the object we are reading
        size: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4)
        new_object = {
            "type_id": type_id_32,
            "position": position,
            "size": size,
            "metadata": {}
        }

        # If this object is of an unknown type, we ignore it
        if type_id not in self.object_types:
            new_object["type_name"] = "Undefined type {:d}".format(type_id)
            return new_object

        # Populate the name of the type of this object
        type_info: dict = self.object_types[type_id]
        new_object["type_name"] = type_info["name"]

        # Populate the bounding box of this object
        if type_info["bbox"]:
            new_object["x_min"] = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 8)
            new_object["y_min"] = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 12)
            new_object["x_max"] = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 16)
            new_object["y_max"] = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 20)

            if type_info["bbox_include_in_render"]:
                self.factor_into_bbox(x=new_object["x_min"], y=new_object["y_min"])
                self.factor_into_bbox(x=new_object["x_max"], y=new_object["y_max"])

            payload_start: int = position + 24
        else:
            payload_start: int = position + 8

        # Read fields
        if "fields" in type_info:
            for field_name, field_props in type_info["fields"].items():
                value = None
                if field_props[1] == "uint":
                    # Unsigned integer
                    value = bytes_to_uint(byte_array=self.bytes, size=field_props[2],
                                          position=payload_start + field_props[0])
                if field_props[1] == "int/65536":
                    # Fixed-point number &XXXX.XXXX
                    value = bytes_to_uint(byte_array=self.bytes, size=field_props[2],
                                          position=payload_start + field_props[0]) / 65536.
                    # Deal with negative values
                    if value > 0x8000:
                        value -= 0x10000
                elif field_props[1] == "str":
                    if field_props[2] > 0:
                        # String of pre-defined length
                        start = payload_start + field_props[0]
                        value = self.bytes[start:start + field_props[2]].decode(encoding='iso-8859-1', errors='replace')
                    else:
                        # Null-terminated string
                        start = payload_start + field_props[0]
                        value = self.bytes[start:].split(b"\x00")[0].decode(encoding='iso-8859-1', errors='replace')
                    # Remove padding
                    value = value.strip()
                # Set metadata item value
                new_object["metadata"][field_name] = value

        # Read path components
        if type_id == 2:
            has_dash_pattern: bool = (new_object["metadata"]["path_style"] & 128) != 0
            new_object["metadata"]["has_dash_pattern"] = has_dash_pattern

            if has_dash_pattern:
                new_object["dash_pattern"] = self.fetch_dash_pattern(position=position + 40)
                payload_start: int = position + 40 + new_object["dash_pattern"]["size"]
            else:
                payload_start: int = position + 40

            new_object["path"] = self.fetch_path(position=payload_start)

        # Read any children this object may have
        if "children_start" in type_info:
            new_object["children"] = []
            self.fetch_objects(target=new_object["children"],
                               position=payload_start + type_info["children_start"],
                               end_position=position + size,
                               exit_on_zero=True)

        # Return this object
        return new_object

    def fetch_dash_pattern(self, position: int) -> dict:
        """
        Fetch a dash pattern from within a path object.

        :param position:
            The byte position of the start of the dash pattern
        :return:
            Dictionary of properties
        """

        # Create dictionary describing the dash pattern we are reading
        start: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 0)
        item_count: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4)
        new_object = {
            "start": start,
            "item_count": item_count,
            "sequence": []
        }

        # Calculate the size of this dash pattern
        size: int = 8 + 4 * item_count
        new_object["size"] = size

        # Read dash pattern
        new_object["sequence"] = [bytes_to_uint(size=4, byte_array=self.bytes, position=position + 8 + 4 * index)
                                  for index in range(item_count)]

        # Return this dash pattern descriptor
        return new_object

    def fetch_path(self, position: int) -> List[Dict]:
        """
        Fetch a path from within a path object.

        :param position:
            The byte position of the start of the path
        :return:
            List of dictionaries of properties
        """

        new_path: List[Dict] = []

        terminate: bool = False
        while not terminate:
            element_type: int = bytes_to_uint(size=4, byte_array=self.bytes, position=position)

            length: Optional[int] = None
            new_component: Optional[dict] = None
            if element_type == 0:
                new_component = {'type': 'END'}
                length = 4
                terminate = True
            elif element_type == 2:
                new_component = {'type': 'MOVE',
                                 'x': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4),
                                 'y': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 8)
                                 }
                self.factor_into_bbox(x=new_component['x'], y=new_component['y'])
                length = 12
            elif element_type == 5:
                new_component = {'type': 'CLOSE'}
                length = 4
            elif element_type == 6:
                new_component = {'type': 'BEZIER',
                                 'x0': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4),
                                 'y0': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 8),
                                 'x1': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 12),
                                 'y1': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 16),
                                 'x2': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 20),
                                 'y2': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 24),
                                 }
                self.factor_into_bbox(x=new_component['x0'], y=new_component['y0'])
                self.factor_into_bbox(x=new_component['x1'], y=new_component['y1'])
                self.factor_into_bbox(x=new_component['x2'], y=new_component['y2'])
                length = 28
            elif element_type == 8:
                new_component = {'type': 'LINE',
                                 'x': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 4),
                                 'y': bytes_to_uint(size=4, byte_array=self.bytes, position=position + 8)
                                 }
                self.factor_into_bbox(x=new_component['x'], y=new_component['y'])
                length = 12

            # If we got an item we can't parse, then finish gracefully
            if length is None:
                new_component = {'type': 'ILLEGAL'}
                length = 4
                terminate = True

            # Add this path element to the chain
            new_path.append(new_component)
            # Advance to next path element
            position += length

        # Return this path
        return new_path

    def describe_path(self, item: list, indent: int = 0) -> str:
        """
        Return a string describing the internal structure of a single path within a Drawfile.

        :param item:
            The list of elements describing the path we are to describe.
        :param indent:
            The number of indentation levels to the left of the text.
        :return:
            str
        """
        output = ""
        tab = "    " * indent
        output += "{:s}* Path has {:d} elements: {}\n".format(tab, len(item), repr(item))

        # Return string describing this path
        return output

    def describe_object(self, item: dict, indent: int = 0) -> str:
        """
        Return a string describing the internal structure of a single object within a Drawfile.

        :param item:
            The dictionary describing the object we are to describe.
        :param indent:
            The number of indentation levels to the left of the text.
        :return:
            str
        """
        output: str = ""
        tab: str = "    " * indent
        output += "{:s}* Object <{:s}>\n".format(tab, item["type_name"])
        output += "{:s}    * Type id       : {:08X}\n".format(tab, item["type_id"])
        output += "{:s}    * Byte position : {:d}\n".format(tab, item["position"])
        output += "{:s}    * Byte size     : {:d}\n".format(tab, item["size"])

        # Render object bounding box
        if "x_min" in item:
            output += "{:s}    * Bounding box X: {:8d} -> {:8d}\n".format(tab, item["x_min"], item["x_max"])
            output += "{:s}    * Bounding box Y: {:8d} -> {:8d}\n".format(tab, item["y_min"], item["y_max"])

        # Render object metadata
        for item_key in sorted(item["metadata"].keys()):
            item_value = item["metadata"][item_key]
            if "colour" in item_key.lower():
                item_value = "{:08X}".format(item_value)
            output += "{:s}    * {:14s}: {}\n".format(tab, item_key, str(item_value))

        # Render path, if present
        if "path" in item:
            output += self.describe_path(item=item["path"], indent=indent + 1)

        # Render object children
        if "children" in item:
            for item in item["children"]:
                output += self.describe_object(item=item, indent=indent + 1)

        # Return string describing this object
        return output

    def describe_contents(self) -> str:
        """
        Return a string describing the internal structure of this Drawfile

        :return:
            str
        """
        output = ""
        output += "File size     : {:d} bytes\n".format(self.size)
        output += "Draw ID       : {:s}\n".format(self.draw_id)
        output += "Major version : {:d}\n".format(self.major_version)
        output += "Minor version : {:d}\n".format(self.minor_version)
        output += "Generator     : {:s}\n".format(self.generator)
        output += "Bounding box X: {:8d} -> {:8d}\n".format(self.x_min_as_read, self.x_max_as_read)
        output += "Bounding box Y: {:8d} -> {:8d}\n".format(self.y_min_as_read, self.y_max_as_read)
        output += "Bounding box X: {:8d} -> {:8d} (computed)\n".format(self.x_min, self.x_max)
        output += "Bounding box Y: {:8d} -> {:8d} (computed)\n".format(self.y_min, self.y_max)

        for item in self.objects:
            output += self.describe_object(item=item)

        return output

    def render_object(self, item: dict, context: GraphicsContext) -> None:
        # Render text objects
        if item['type_name'] in ("Text object", "Transformed text object"):
            context.set_font_size(font_size=1)
            text_string: str = item["metadata"]["text"]
            text_extent = context.measure_text(text=text_string)
            if text_extent["width"] == 0:
                logging.info("Ignoring text item <{}> with zero width".format(text_string))
                return
            text_colour: Sequence[float] = context_colour_from_int(item["metadata"]["text_colour"])
            x_centre: float = self.x_pos(x=(item["x_max"] + item["x_min"]) / 2)
            y_centre: float = self.y_pos(y=(item["y_max"] + item["y_min"]) / 2)
            target_width: float = (item["x_max"] - item["x_min"]) * self.pixel
            target_height: float = (item["y_max"] - item["y_min"]) * self.pixel
            context.set_color(color=text_colour)
            if item['type_name'] == "Transformed text object":
                # Apply transformation to sprite
                xx: float = item["metadata"]["transformation_a"]
                yx: float = -item["metadata"]["transformation_b"]
                xy: float = -item["metadata"]["transformation_c"]
                yy: float = item["metadata"]["transformation_d"]
                context.matrix_transformation_set(xx=xx, yx=yx, xy=xy, yy=yy, x0=0, y0=0,
                                                  centre_x=x_centre, centre_y=y_centre
                                                  )

                # Work out the correct scaling to fill the bounding box
                corners = [(0.5 * sgn_x, 0.5 * sgn_y) for sgn_x in (-1, 1) for sgn_y in (-1, 1)]
                corners_transformed = [(p[0] * xx + p[1] * xy, p[0] * yx + p[1] * yy) for p in corners]
                transformed_unit_width = (max([p[0] for p in corners_transformed]) -
                                          min([p[0] for p in corners_transformed]))
                target_width_transformed = target_width / transformed_unit_width
                target_height_transformed = target_height / transformed_unit_width

                # Paint text
                font_size: float = target_width_transformed / text_extent["width"]
                context.set_font_size(font_size=font_size)
                context.text(text=text_string, h_align=0, v_align=0, gap=0, rotation=0, x=0, y=0)

                # Undo transformation
                context.matrix_transformation_restore()
            else:
                font_size: float = target_width / text_extent["width"]
                context.set_font_size(font_size=font_size)
                context.text(text=text_string, h_align=0, v_align=0, gap=0, rotation=0, x=x_centre, y=y_centre)

        # Render path objects
        if item['type_name'] == "Path object":
            # Start path
            context.begin_path()

            # Trace path, point by point
            for path_item in item['path']:
                if path_item['type'] == 'END':
                    break
                elif path_item['type'] == 'MOVE':
                    context.move_to(x=self.x_pos(x=path_item['x']), y=self.y_pos(y=path_item['y']))
                elif path_item['type'] == 'CLOSE':
                    context.close_path()
                    context.begin_sub_path()
                elif path_item['type'] == 'BEZIER':
                    context.curve_to(x0=self.x_pos(x=path_item['x0']), y0=self.y_pos(y=path_item['y0']),
                                     x1=self.x_pos(x=path_item['x1']), y1=self.y_pos(y=path_item['y1']),
                                     x2=self.x_pos(x=path_item['x2']), y2=self.y_pos(y=path_item['y2']))
                elif path_item['type'] == 'LINE':
                    context.line_to(x=self.x_pos(x=path_item['x']), y=self.y_pos(y=path_item['y']))

            # Fill path
            fill_colour = context_colour_from_int(uint=item['metadata']['fill_colour'])
            if fill_colour[3] > 0:
                context.fill(color=fill_colour)

            # Stroke path
            stroke_colour = context_colour_from_int(uint=item['metadata']['outline_colour'])
            outline_width = max(2, item['metadata']['outline_width'] * self.pixel / context.base_line_width)
            if stroke_colour[3] > 0:
                context.stroke(color=stroke_colour, line_width=outline_width,
                               dotted=item['metadata']['has_dash_pattern'])

        # Render sprite objects
        if item['type_name'] in ("Sprite object", "Transformed sprite object"):
            if item['type_name'] == "Sprite object":
                preface_size = 24
            else:
                preface_size = 48
            block_position = item["position"] + preface_size  # Start of sprite data
            block_size = item["size"] - preface_size  # Number of bytes of sprite data
            sprite_bytes = self.bytes[block_position:block_position + block_size]

            # Construct a sprite file containing this sprite
            sprite_file_handle = io.BytesIO()
            # Number of sprites in area
            sprite_file_handle.write((1).to_bytes(length=4, byteorder='little'))
            # Offset to first sprite
            sprite_file_handle.write((0x10).to_bytes(length=4, byteorder='little'))
            # Offset to first free word in area (i.e. after last sprite)
            free = bytes_to_uint(size=4, byte_array=sprite_bytes, position=0) + 0x10
            sprite_file_handle.write(free.to_bytes(length=4, byteorder='little'))
            # Sprite data
            sprite_file_handle.write(sprite_bytes)

            # Convert it into a sprite object
            try:
                sprite = spritefile.spritefile(file=sprite_file_handle)
                with temporary_directory.TemporaryDirectory() as tmp_dir:
                    spr2img.convert_sprites(spr=sprite, output_dir=tmp_dir.tmp_dir, format="png")
                    first_sprite = glob.glob(os.path.join(tmp_dir.tmp_dir, "*.png"))[0]

                    # Render sprite
                    if item['type_name'] == "Transformed sprite object":
                        centre_x: float = self.x_pos((item["x_max"] + item["x_min"]) / 2)
                        centre_y: float = self.y_pos((item["y_max"] + item["y_min"]) / 2)
                        target_width: float = (item["x_max"] - item["x_min"]) * self.pixel
                        target_height: float = (item["y_max"] - item["y_min"]) * self.pixel

                        # Apply transformation to sprite
                        xx: float = item["metadata"]["transformation_a"]
                        yx: float = -item["metadata"]["transformation_b"]
                        xy: float = -item["metadata"]["transformation_c"]
                        yy: float = item["metadata"]["transformation_d"]
                        context.matrix_transformation_set(xx=xx, yx=yx, xy=xy, yy=yy, x0=0, y0=0,
                                                          centre_x=centre_x, centre_y=centre_y
                                                          )

                        # Work out the correct scaling to fill the bounding box
                        corners = [(0.5 * sgn_x, 0.5 * sgn_y) for sgn_x in (-1, 1) for sgn_y in (-1, 1)]
                        corners_transformed = [(p[0] * xx + p[1] * xy, p[0] * yx + p[1] * yy) for p in corners]
                        transformed_unit_width = (max([p[0] for p in corners_transformed]) -
                                                  min([p[0] for p in corners_transformed]))
                        target_width_transformed = target_width / transformed_unit_width
                        target_height_transformed = target_height / transformed_unit_width

                        # Paint sprite onto the canvas
                        context.paint_png_image(png_filename=first_sprite,
                                                x_left=-target_width_transformed / 2,
                                                y_top=-target_height_transformed / 2,
                                                target_width=target_width_transformed,
                                                target_height=target_height_transformed
                                                )

                        # Undo transformation
                        context.matrix_transformation_restore()
                    else:
                        # Paint sprite onto the canvas
                        context.paint_png_image(png_filename=first_sprite,
                                                x_left=self.x_pos(x=item["x_min"]),
                                                y_top=self.y_pos(y=item["y_max"]),
                                                target_width=(item["x_max"] - item["x_min"]) * self.pixel,
                                                target_height=(item["y_max"] - item["y_min"]) * self.pixel
                                                )

            except:
                logging.info("Failed to render sprite")

        # Render object children
        if "children" in item:
            for item in item["children"]:
                self.render_object(item=item, context=context)

    def render_to_context(self, filename: str, img_format: str, dots_per_inch: float = 72.) -> None:
        """
        Render this Draw file to a graphics page.
        """

        with GraphicsPage(img_format=img_format, output=filename, dots_per_inch=dots_per_inch,
                          width=(self.x_max - self.x_min) * self.pixel,
                          height=(self.y_max - self.y_min) * self.pixel
                          ) as page:
            with GraphicsContext(page=page, offset_x=0, offset_y=0) as context:
                for item in self.objects:
                    self.render_object(item=item, context=context)


# Do it right away if we're run as a script
if __name__ == "__main__":
    # Read input parameters
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',
                        default="my_drawfile.aff",
                        type=str,
                        dest="input_filename",
                        help="Input Draw file to process")
    parser.add_argument('--output',
                        default="/tmp/my_drawfile",
                        type=str,
                        dest="output_filename",
                        help="Output destination for PNG output")
    parser.add_argument('--debug',
                        action='store_true',
                        dest="debug",
                        help="Show full debugging output")
    parser.set_defaults(debug=False)
    args = parser.parse_args()

    # Set up a logging object
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s:%(filename)s:%(message)s',
                        datefmt='%d/%m/%Y %H:%M:%S')
    logger = logging.getLogger(__name__)
    logger.debug(__doc__.strip())

    # Open input file
    df = DrawFileRender(filename=args.input_filename)
    print(df.describe_contents())
    df.render_to_context(filename=args.output_filename, img_format="png")
