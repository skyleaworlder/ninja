#!/usr/bin/env python
#
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Write-only protobuf C++ code generator for a minimal runtime

This script uses a descriptor proto generated by protoc and the descriptor_pb2
distributed with python protobuf to iterate through the fields in a proto
and write out simple C++ data objects with serialization methods.  The generated
files depend on a tiny runtime implemented in src/proto.h and src/proto.cc.
"""

from __future__ import print_function

import os.path
import re
import sys

import google.protobuf.descriptor_pb2
import google.protobuf.descriptor

FieldDescriptor = google.protobuf.descriptor.FieldDescriptor

CPP_TYPE_MAP = {
    FieldDescriptor.CPPTYPE_INT32: 'int32_t',
    FieldDescriptor.CPPTYPE_INT64: 'int64_t',
    FieldDescriptor.CPPTYPE_UINT32: 'uint32_t',
    FieldDescriptor.CPPTYPE_UINT64: 'uint64_t',
    FieldDescriptor.CPPTYPE_DOUBLE: 'double',
    FieldDescriptor.CPPTYPE_FLOAT: 'float',
    FieldDescriptor.CPPTYPE_BOOL: 'bool',
    FieldDescriptor.CPPTYPE_STRING: 'std::string',
}

ENCODING_MAP = {
    FieldDescriptor.TYPE_INT32:
        ('VarintSize32SignExtended', 'WriteVarint32SignExtended', None),
    FieldDescriptor.TYPE_INT64:
        ('VarintSize64', 'WriteVarint64', None),
    FieldDescriptor.TYPE_UINT32:
        ('VarintSize32', 'WriteVarint32', None),
    FieldDescriptor.TYPE_UINT64:
        ('VarintSize64', 'WriteVarint64', None),
    FieldDescriptor.TYPE_SINT32:
        ('VarintSize32', 'WriteVarint32', 'ZigZagEncode32'),
    FieldDescriptor.TYPE_SINT64:
        ('VarintSize64', 'WriteVarint64', 'ZigZagEncode64'),
    FieldDescriptor.TYPE_BOOL:
        ('VarintSizeBool', 'WriteVarint32', None),
    FieldDescriptor.TYPE_ENUM:
        ('VarintSize32SignExtended', 'WriteVarint32SignExtended',
         'static_cast<int32_t>'),
    FieldDescriptor.TYPE_FIXED64:
        ('FixedSize64', 'WriteFixed64', None),
    FieldDescriptor.TYPE_SFIXED64:
        ('FixedSize64', 'WriteFixed64', 'static_cast<uint64_t>'),
    FieldDescriptor.TYPE_DOUBLE:
        ('FixedSize64', 'WriteFixed64', 'static_cast<uint64_t>'),
    FieldDescriptor.TYPE_STRING:
        ('StringSize', 'WriteString', None),
    FieldDescriptor.TYPE_BYTES:
        ('StringSize', 'WriteString', None),
    FieldDescriptor.TYPE_FIXED32:
        ('FixedSize32', 'WriteFixed32', None),
    FieldDescriptor.TYPE_SFIXED32:
        ('FixedSize32', 'WriteFixed32', 'static_cast<uint32_t>'),
    FieldDescriptor.TYPE_FLOAT:
        ('FixedSize32', 'WriteFixed32', 'static_cast<uint32_t>'),
}

ZIGZAG_LIST = (
    FieldDescriptor.TYPE_SINT32,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_SFIXED64,
    FieldDescriptor.TYPE_SFIXED32,
)

def field_type_to_cpp_type(field):
    """Convert a proto field object to its C++ type"""
    if field.type_name != '':
        cpp_type = field.type_name.replace('.', '::')
    else:
        cpp_type = FieldDescriptor.ProtoTypeToCppProtoType(field.type)
        cpp_type = CPP_TYPE_MAP[cpp_type]
    return cpp_type

class Generator:
    def __init__(self, out):
        self.w = Writer(out)

    """Class to generate C++ code for a proto descriptor"""
    def write_enum(self, enum):
        """Write a proto enum object to the generated file"""
        self.w.writelines("""
            enum %(name)s {
        """ % {
            'name': enum.name,
        })
        self.w.indent()
        for value in enum.value:
            self.w.writelines("""
                %(name)s = %(value)d,
            """ % {
                'name': value.name,
                'value': value.number,
            })
        self.w.unindent()
        self.w.writelines("""
        };

        """)

    def write_field(self, field, ctor, ser, size, clear, methods):
        """Write a proto field object to the generated file, including necessary
        code in the constructor and serialization methods.
        """
        field_cpp_type = field_type_to_cpp_type(field)
        repeated = field.label == FieldDescriptor.LABEL_REPEATED

        element_cpp_type = field_cpp_type
        if repeated:
            field_cpp_type = 'std::vector< %s >' % field_cpp_type

        member_name = field.name + '_'
        element_name = member_name

        # Data declaration
        self.w.writelines("""
            %(type)s %(member_name)s;
            bool has_%(member_name)s;
        """ % {
            'type': field_cpp_type,
            'member_name': member_name,
        })

        ctor.writelines("""
            has_%(member_name)s = false;
        """ % {
            'member_name': member_name,
        })

        methods.writelines("""
                %(type)s* mutable_%(name)s() {
                  has_%(member_name)s = true;
                  return &%(member_name)s;
                }
            """ % {
            'name': field.name,
            'member_name': member_name,
            'type': field_cpp_type,
        })

        if repeated:
            loop = """
                for (%(type)s::const_iterator it_ = %(member_name)s.begin();
                    it_ != %(member_name)s.end(); it_++) {
            """ % {
                'member_name': member_name,
                'type': field_cpp_type,
            }

            ser.writelines(loop)
            ser.indent()

            size.writelines(loop)
            size.indent()

            methods.writelines("""
                void add_%(name)s(const %(type)s& value) {
                  has_%(member_name)s = true;
                  %(member_name)s.push_back(value);
                }
            """ % {
                'name': field.name,
                'member_name': member_name,
                'type': element_cpp_type,
            })

            element_name = '*it_'

        if field.type == FieldDescriptor.TYPE_MESSAGE:
            ser.writelines("""
                if (has_%(member_name)s) {
                  WriteLengthDelimited(output__, %(number)s,
                                       %(member_name)s.ByteSizeLong());
                  %(member_name)s.SerializeToOstream(output__);
                }
            """ % {
                'member_name': element_name,
                'number': field.number,
            })

            size.writelines("""
                if (has_%(member_name)s) {
                  size += 1 + VarintSize32(%(member_name)s.ByteSizeLong());
                  size += %(member_name)s.ByteSizeLong();
                }
            """ % {
                'member_name': element_name,
            })

            clear.writelines("""
                if (has_%(member_name)s) {
                  %(member_name)s.Clear();
                  has_%(member_name)s = false;
                }
            """ % {
                'member_name': member_name,
            })
        else:
            (sizer, serializer, formatter) = ENCODING_MAP[field.type]
            if formatter != None:
                element_name = '%s(%s)' % (formatter, element_name)

            ser.writelines("""
                %(serializer)s(output__, %(field_number)s, %(element_name)s);
            """ % {
                'serializer': serializer,
                'field_number': field.number,
                'element_name': element_name,
            })

            size.writelines("""
                size += %(sizer)s(%(element_name)s) + 1;
            """ % {
                'sizer': sizer,
                'element_name': element_name,
            })

            if repeated or field.type == FieldDescriptor.CPPTYPE_STRING:
                clear.writelines("""
                    %(member_name)s.clear();
                """ % {
                    'member_name': member_name,
                })
            else:
                reset = """
                    %(member_name)s = static_cast< %(type)s >(0);
                """ % {
                    'member_name': member_name,
                    'type': field_cpp_type,
                }
                ctor.writelines(reset)
                clear.writelines(reset)

            methods.writelines("""
                void set_%(name)s(const %(type)s& value) {
                  has_%(member_name)s = true;
                  %(member_name)s = value;
                }
            """ % {
                'name': field.name,
                'member_name': member_name,
                'type': field_cpp_type,
            })

        if repeated:
            ser.unindent()
            ser.writelines('}')
            size.unindent()
            size.writelines('}')

    def func(self, f):
        return self.w.stringwriter(prefix=f + ' {', suffix='}\n\n')

    def write_message(self, message):
        """Write a proto message object to the generated file, recursing into
        nested messages, enums, and fields.
        """
        self.w.writelines("""
            struct %(name)s {
        """ % {
            'name': message.name,
        })
        self.w.indent()

        # Constructor
        ctor = self.func(message.name + '()')

        # SerializeToOstream method
        ser = self.func('void SerializeToOstream(std::ostream* output__) const')

        size = self.func('size_t ByteSizeLong() const')
        size.writelines("""
            size_t size = 0;
        """)

        clear = self.func('void Clear()')

        methods = self.w.stringwriter()

        # Nested message type declarations
        for nested in message.nested_type:
            self.write_message(nested)

        # Nested enum type declarations
        for enum in message.enum_type:
            self.write_enum(enum)

        # Message fields
        for field in message.field:
            self.write_field(field, ctor, ser, size, clear, methods)
        if len(message.field) > 0:
            self.w.newline()

        self.w.writelines(ctor.string())

        # Disallow copy and assign constructors
        self.w.writelines("""
            %(name)s(const %(name)s&);
            void operator=(const %(name)s&);

        """ % {
            'name': message.name,
        })

        # SerializeToOstream method
        self.w.writelines(ser.string())

        # ByteSizeLong method
        size.writelines('return size;')
        self.w.writelines(size.string())

        # Clear method
        self.w.writelines(clear.string())

        # Accessor methods
        self.w.write(methods.string())

        self.w.unindent()
        self.w.writelines("""
            };

        """)

    def write_proto(self, output_file, proto):
        header_guard = 'NINJA_' + os.path.basename(output_file).upper()
        header_guard = re.sub('[^a-zA-Z]', '_', header_guard)

        self.w.writelines("""
            // This file is autogenerated by %(generator)s, do not edit

            #ifndef %(header_guard)s
            #define %(header_guard)s

            #include <inttypes.h>

            #include <iostream>
            #include <string>
            #include <vector>

            #include "proto.h"

            namespace %(namespace)s {
        """ % {
            'generator': os.path.basename(sys.argv[0]),
            'header_guard': header_guard,
            'namespace': proto.package,
        })

        for enum in proto.enum_type:
            self.write_enum(enum)
        for message in proto.message_type:
            self.write_message(message)

        self.w.writelines("""
        }
        #endif // %(header_guard)s
        """ % {
          'header_guard': header_guard,
        })

class Writer:
    """Class to write code to a generated file"""
    def __init__(self, w, indent=0):
        self.w = w
        self.cur_indent = indent

    def write(self, s):
        self.w.write(s)

    def writeln(self, s):
        if len(s) > 0:
            self.write(' '*self.cur_indent + s + '\n')
        else:
            self.newline()

    def indent(self):
        self.cur_indent = self.cur_indent + 2

    def unindent(self):
        self.cur_indent = self.cur_indent - 2

    def newline(self):
        self.write('\n')

    def writelines(self, s):
        lines = s.split('\n')
        if len(lines) > 0:
            if len(lines[0].strip()) == 0:
                lines = lines[1:]
        if len(lines) > 0:
            first_indent = initial_indent(lines[0])

            for line in lines[:-1]:
                indent = min(initial_indent(line), first_indent)
                self.writeln(line[indent:])

            indent = min(initial_indent(lines[-1]), first_indent)
            if lines[-1][indent:] != '':
                self.writeln(lines[-1][indent:])

    def stringwriter(self, prefix='', suffix=''):
        """Returns an object with the same interface as Writer that buffers
        its writes to be written out later.
        """
        return StringWriter(self.cur_indent, prefix, suffix)

def initial_indent(s):
    return len(s)-len(s.lstrip(' '))

class StringWriter(Writer):
    """Subclass of Writer that buffers its writes to be written out later."""
    def __init__(self, indent, prefix, suffix):
        self.buf = ''
        self.prefix = prefix
        self.suffix = suffix
        self.cur_indent = indent
        if self.prefix != '':
            self.writelines(self.prefix)
            self.indent()

    def string(self):
        if self.prefix != '':
            self.unindent()
            if self.suffix != '':
                self.writelines(self.suffix)
        return self.buf

    def write(self, s):
        self.buf += s

def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--probe':
        print('ok')
        return

    if len(sys.argv) != 3:
        print('usage: %s <in> <out>' % sys.argv[0])
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2]
    tmp_output_file = output_file + '.tmp'

    set = google.protobuf.descriptor_pb2.FileDescriptorSet()
    try:
        with open(input_file, 'rb') as f:
            set.ParseFromString(f.read())
    except IOError:
        print('failed to read ' + input_file)
        sys.exit(2)

    if len(set.file) != 1:
        print('expected exactly one file descriptor in ' + input_file)
        print(set)
        sys.exit(3)

    proto = set.file[0]

    with open(tmp_output_file, 'w') as out:
        w = Generator(out)

        w.write_proto(output_file, proto)

    os.rename(tmp_output_file, output_file)

if __name__ == '__main__':
    main()
