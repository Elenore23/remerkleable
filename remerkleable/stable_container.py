# This file implements `StableContainer` according to https://eips.ethereum.org/EIPS/eip-7495
# The EIP is still under review, functionality may change or go away without deprecation.

import io
from typing import Any, BinaryIO, Dict, List as PyList, Optional, Tuple, \
    TypeVar, Type, Union as PyUnion, \
    get_args, get_origin
from textwrap import indent
from remerkleable.bitfields import Bitvector
from remerkleable.complex import ComplexView, Container, FieldOffset, \
    decode_offset, encode_offset
from remerkleable.core import View, ViewHook, ViewMeta, OFFSET_BYTE_LENGTH
from remerkleable.tree import Gindex, NavigationError, Node, PairNode, \
    get_depth, subtree_fill_to_contents, zero_node, \
    RIGHT_GINDEX

N = TypeVar('N', bound=int)
SV = TypeVar('SV', bound='ComplexView')
BV = TypeVar('BV', bound='ComplexView')


def stable_get(self, findex, ftyp, n):
    if not self.active_fields().get(findex):
        return None
    data = self.get_backing().get_left()
    fnode = data.getter(2**get_depth(n) + findex)
    return ftyp.view_from_backing(fnode)


def stable_set(self, findex, ftyp, n, value):
    next_backing = self.get_backing()

    active_fields = self.active_fields()
    active_fields.set(findex, value is not None)
    next_backing = next_backing.rebind_right(active_fields.get_backing())

    if value is not None:
        if isinstance(value, ftyp):
            fnode = value.get_backing()
        else:
            fnode = ftyp.coerce_view(value).get_backing()
    else:
        fnode = zero_node(0)
    data = next_backing.get_left()
    next_data = data.setter(2**get_depth(n) + findex)(fnode)
    next_backing = next_backing.rebind_left(next_data)

    self.set_backing(next_backing)


def field_val_repr(self, fkey: str, ftyp: Type[View], fopt: bool) -> str:
    field_start = '  ' + fkey + ': ' + (
        ('Optional[' if fopt else '') + ftyp.__name__ + (']' if fopt else '')
    ) + ' = '
    try:
        field_repr = getattr(self, fkey).__repr__()
        if '\n' in field_repr:  # if multiline, indent it, but starting from the value.
            i = field_repr.index('\n')
            field_repr = field_repr[:i+1] + indent(field_repr[i+1:], ' ' * len(field_start))
        return field_start + field_repr
    except NavigationError:
        return f'{field_start} *omitted*'


class StableContainer(ComplexView):
    __slots__ = '_field_indices', 'N'
    _field_indices: Dict[str, Tuple[int, Type[View]]]
    N: int

    def __new__(cls, backing: Optional[Node] = None, hook: Optional[ViewHook] = None, **kwargs):
        if backing is not None:
            if len(kwargs) != 0:
                raise Exception('Cannot have both a backing and elements to init fields')
            return super().__new__(cls, backing=backing, hook=hook, **kwargs)

        input_nodes = []
        active_fields = Bitvector[cls.N]()
        for fkey, (findex, ftyp) in cls._field_indices.items():
            fnode: Node
            finput = kwargs.pop(fkey) if fkey in kwargs else None
            if finput is None:
                fnode = zero_node(0)
                active_fields.set(findex, False)
            else:
                if isinstance(finput, View):
                    fnode = finput.get_backing()
                else:
                    fnode = ftyp.coerce_view(finput).get_backing()
                active_fields.set(findex, True)
            input_nodes.append(fnode)
        if len(kwargs) > 0:
            raise AttributeError(f'Fields [{"".join(kwargs.keys())}] unknown in `{cls.__name__}`')

        backing = PairNode(
            left=subtree_fill_to_contents(input_nodes, get_depth(cls.N)),
            right=active_fields.get_backing(),
        )
        return super().__new__(cls, backing=backing, hook=hook, **kwargs)

    def __init_subclass__(cls, **kwargs):
        if 'n' not in kwargs:
            raise TypeError(f'Missing capacity: `{cls.__name__}(StableContainer)`')
        n = kwargs.pop('n')
        if not isinstance(n, int):
            raise TypeError(f'Invalid capacity: `{cls.__name__}(StableContainer[{n}])`')
        if n <= 0:
            raise TypeError(f'Unsupported capacity: `{cls.__name__}(StableContainer[{n}])`')
        cls.N = n

    def __class_getitem__(cls, n: int) -> Type['StableContainer']:
        class StableContainerMeta(ViewMeta):
            def __new__(cls, name, bases, dct):
                return super().__new__(cls, name, bases, dct, n=n)

        class StableContainerView(StableContainer, metaclass=StableContainerMeta):
            def __init_subclass__(cls, **kwargs):
                if 'N' in cls.__dict__:
                    raise TypeError(f'Cannot override `N` inside `{cls.__name__}`')
                cls._field_indices = {}
                for findex, (fkey, t) in enumerate(cls.__annotations__.items()):
                    if (
                        get_origin(t) != PyUnion
                        or len(get_args(t)) != 2
                        or type(None) not in get_args(t)
                    ):
                        raise TypeError(
                            f'`StableContainer` fields must be `Optional[T]` '
                            f'but `{cls.__name__}.{fkey}` has type `{t.__name__}`'
                        )
                    ftyp = get_args(t)[0] if get_args(t)[0] is not type(None) else get_args(t)[1]
                    cls._field_indices[fkey] = (findex, ftyp)
                if len(cls._field_indices) > cls.N:
                    raise TypeError(
                        f'`{cls.__name__}` is `StableContainer[{cls.N}]` '
                        f'but contains {len(cls._field_indices)} fields'
                    )

        StableContainerView.__name__ = StableContainerView.type_repr()
        return StableContainerView

    @classmethod
    def fields(cls) -> Dict[str, Type[View]]:
        return { fkey: ftyp for fkey, (_, ftyp) in cls._field_indices.items() }

    @classmethod
    def is_fixed_byte_length(cls) -> bool:
        return False

    @classmethod
    def min_byte_length(cls) -> int:
        return Bitvector[cls.N].type_byte_length()

    @classmethod
    def max_byte_length(cls) -> int:
        total = Bitvector[cls.N].type_byte_length()
        for (_, ftyp) in cls._field_indices.values():
            if not ftyp.is_fixed_byte_length():
                total += OFFSET_BYTE_LENGTH
            total += ftyp.max_byte_length()
        return total

    @classmethod
    def is_packed(cls) -> bool:
        return False

    @classmethod
    def tree_depth(cls) -> int:
        return get_depth(cls.N)

    @classmethod
    def item_elem_cls(cls, i: int) -> Type[View]:
        return list(cls._field_indices.values())[i]

    def active_fields(self) -> Bitvector:
        active_fields_node = super().get_backing().get_right()
        return Bitvector[self.__class__.N].view_from_backing(active_fields_node)

    def __getattribute__(self, item):
        if item == 'N':
            raise AttributeError(f'Use `.__class__.{item}` to access `{item}`')
        return object.__getattribute__(self, item)

    def __getattr__(self, item):
        if item[0] == '_':
            return super().__getattribute__(item)
        else:
            try:
                (findex, ftyp) = self.__class__._field_indices[item]
            except KeyError:
                raise AttributeError(f'Unknown field `{item}`')

            return stable_get(self, findex, ftyp, self.__class__.N)

    def __setattr__(self, key, value):
        if key[0] == '_':
            super().__setattr__(key, value)
        else:
            try:
                (findex, ftyp) = self.__class__._field_indices[key]
            except KeyError:
                raise AttributeError(f'Unknown field `{key}`')

            stable_set(self, findex, ftyp, self.__class__.N, value)

    def __repr__(self):
        return f'{self.__class__.type_repr()}:\n' + '\n'.join(
            indent(field_val_repr(self, fkey, ftyp, fopt=True), '  ')
            for fkey, (_, ftyp) in self.__class__._field_indices.items())

    @classmethod
    def type_repr(cls) -> str:
        return f'StableContainer[{cls.N}]'

    @classmethod
    def deserialize(cls: Type[SV], stream: BinaryIO, scope: int) -> SV:
        num_prefix_bytes = Bitvector[cls.N].type_byte_length()
        if scope < num_prefix_bytes:
            raise ValueError(f'Scope too small for `StableContainer[{cls.N}]` active fields')
        active_fields = Bitvector[cls.N].deserialize(stream, num_prefix_bytes)
        scope = scope - num_prefix_bytes

        for findex in range(len(cls._field_indices), cls.N):
            if active_fields.get(findex):
                raise Exception(f'Unknown field index {findex}')

        field_values: Dict[str, View] = {}
        dyn_fields: PyList[FieldOffset] = []
        fixed_size = 0
        for fkey, (findex, ftyp) in cls._field_indices.items():
            if not active_fields.get(findex):
                continue
            if ftyp.is_fixed_byte_length():
                fsize = ftyp.type_byte_length()
                field_values[fkey] = ftyp.deserialize(stream, fsize)
                fixed_size += fsize
            else:
                dyn_fields.append(FieldOffset(
                    key=fkey, typ=ftyp, offset=int(decode_offset(stream))))
                fixed_size += OFFSET_BYTE_LENGTH
        if len(dyn_fields) > 0:
            if dyn_fields[0].offset < fixed_size:
                raise Exception(f'First offset {dyn_fields[0].offset} is '
                                f'smaller than expected fixed size {fixed_size}')
            for i, (fkey, ftyp, foffset) in enumerate(dyn_fields):
                next_offset = dyn_fields[i + 1].offset if i + 1 < len(dyn_fields) else scope
                if foffset > next_offset:
                    raise Exception(f'Offset {i} is invalid: {foffset} '
                                    f'larger than next offset {next_offset}')
                fsize = next_offset - foffset
                f_min_size, f_max_size = ftyp.min_byte_length(), ftyp.max_byte_length()
                if not (f_min_size <= fsize <= f_max_size):
                    raise Exception(f'Offset {i} is invalid, size out of bounds: '
                                    f'{foffset}, next {next_offset}, implied size: {fsize}, '
                                    f'size bounds: [{f_min_size}, {f_max_size}]')
                field_values[fkey] = ftyp.deserialize(stream, fsize)
        return cls(**field_values)  # type: ignore

    def serialize(self, stream: BinaryIO) -> int:
        active_fields = self.active_fields()
        num_prefix_bytes = active_fields.serialize(stream)

        num_data_bytes = 0
        has_dyn_fields = False
        for (findex, ftyp) in self.__class__._field_indices.values():
            if not active_fields.get(findex):
                continue
            if ftyp.is_fixed_byte_length():
                num_data_bytes += ftyp.type_byte_length()
            else:
                num_data_bytes += OFFSET_BYTE_LENGTH
                has_dyn_fields = True

        if has_dyn_fields:
            temp_dyn_stream = io.BytesIO()
        data = super().get_backing().get_left()
        for (findex, ftyp) in self.__class__._field_indices.values():
            if not active_fields.get(findex):
                continue
            fnode = data.getter(2**get_depth(self.__class__.N) + findex)
            v = ftyp.view_from_backing(fnode)
            if ftyp.is_fixed_byte_length():
                v.serialize(stream)
            else:
                encode_offset(stream, num_data_bytes)
                num_data_bytes += v.serialize(temp_dyn_stream)  # type: ignore
        if has_dyn_fields:
            temp_dyn_stream.seek(0)
            stream.write(temp_dyn_stream.read())

        return num_prefix_bytes + num_data_bytes

    @classmethod
    def navigate_type(cls, key: Any) -> Type[View]:
        if key == '__active_fields__':
            return Bitvector[cls.N]
        (_, ftyp) = cls._field_indices[key]
        return Optional[ftyp]

    @classmethod
    def key_to_static_gindex(cls, key: Any) -> Gindex:
        if key == '__active_fields__':
            return RIGHT_GINDEX
        (findex, _) = cls._field_indices[key]
        return 2**get_depth(cls.N) * 2 + findex


class Profile(ComplexView):
    __slots__ = '_field_indices', '_o', 'B'
    _field_indices: Dict[str, Tuple[int, Type[View], bool]]
    _o: int
    B: PyUnion[Type[StableContainer], Type[Container]]

    def __new__(cls, backing: Optional[Node] = None, hook: Optional[ViewHook] = None, **kwargs):
        if backing is not None:
            if len(kwargs) != 0:
                raise Exception('Cannot have both a backing and elements to init fields')
            return super().__new__(cls, backing=backing, hook=hook, **kwargs)

        extra_kw = kwargs.copy()
        for fkey, (_, _, fopt) in cls._field_indices.items():
            if fkey in extra_kw:
                extra_kw.pop(fkey)
            elif not fopt:
                raise AttributeError(f'Field `{fkey}` is required in {cls.__name__}')
            else:
                pass
        if len(extra_kw) > 0:
            raise AttributeError(f'Fields [{"".join(extra_kw.keys())}] unknown in `{cls.__name__}`')

        value = cls.B(backing, hook, **kwargs)
        return cls(backing=value.get_backing())

    def __init_subclass__(cls, **kwargs):
        if 'b' not in kwargs:
            raise TypeError(f'Missing base type: `{cls.__name__}(Profile)`')
        b = kwargs.pop('b')
        if not issubclass(b, StableContainer) and not issubclass(b, Container):
            raise TypeError(f'Invalid base type: `{cls.__name__}(Profile[{b.__name__}])`')
        cls.B = b

    def __class_getitem__(cls, b) -> Type['Profile']:
        class ProfileMeta(ViewMeta):
            def __new__(cls, name, bases, dct):
                return super().__new__(cls, name, bases, dct, b=b)

        class ProfileView(Profile, metaclass=ProfileMeta):
            def __init_subclass__(cls, **kwargs):
                if 'B' in cls.__dict__:
                    raise TypeError(f'Cannot override `B` inside `{cls.__name__}`')
                cls._field_indices = {}
                cls._o = 0
                for (fkey, t) in cls.__annotations__.items():
                    if fkey not in cls.B._field_indices:
                        raise TypeError(
                            f'`{cls.__name__}` fields must exist in the base type '
                            f'but `{fkey}` is not defined in `{cls.B.__name__}`'
                        )
                    if issubclass(cls.B, StableContainer):
                        (findex, ftyp) = cls.B._field_indices[fkey]
                    else:
                        findex = cls.B._field_indices[fkey]
                        ftyp = cls.B.fields()[fkey]
                    fopt = (
                        get_origin(t) == PyUnion
                        and len(get_args(t)) == 2
                        and type(None) in get_args(t)
                    )
                    if fopt:
                        if not issubclass(cls.B, StableContainer):
                            raise TypeError(
                                f'`{cls.__name__}.{fkey}` cannot be `Optional[T]` '
                                f'as base type `{cls.B.__name__}` is not a `StableContainer`'
                            )
                        t = get_args(t)[0] if get_args(t)[0] is not type(None) else get_args(t)[1]
                    if t == ftyp:
                        pass
                    elif issubclass(t, Profile) and t.B == ftyp:
                        pass
                    else:
                        raise TypeError(
                            f'`{cls.__name__}.{fkey}` has type `{t.__name__}`, incompatible '
                            f'with base field `{cls.B.__name__}.{fkey}` of type `{ftyp.__name__}`'
                        )
                    cls._field_indices[fkey] = (findex, t, fopt)
                    if fopt:
                        cls._o += 1
                if (
                    not issubclass(cls.B, StableContainer)
                    and len(cls._field_indices) != len(cls.B._field_indices)
                ):
                    for fkey, (findex, ftyp) in cls.B._field_indices.items():
                        if fkey not in cls._field_indices:
                            raise TypeError(
                                f'`{cls.__name__}.{fkey}` of type `{ftyp.__name__}` is required '
                                f'as base type `{cls.B.__name__}` is not a `StbleContainer`'
                            )

        ProfileView.__name__ = ProfileView.type_repr()
        return ProfileView

    @classmethod
    def fields(cls) -> Dict[str, Tuple[Type[View], bool]]:
        return { fkey: (ftyp, fopt) for fkey, (_, ftyp, fopt) in cls._field_indices.items() }

    @classmethod
    def is_fixed_byte_length(cls) -> bool:
        if cls._o > 0:
            return False
        for (_, ftyp, _) in cls._field_indices.values():
            if not ftyp.is_fixed_byte_length():
                return False
        return True

    @classmethod
    def type_byte_length(cls) -> int:
        if cls.is_fixed_byte_length():
            return cls.min_byte_length()
        else:
            raise Exception(f'Dynamic length `Profile` does not have a fixed byte length')

    @classmethod
    def min_byte_length(cls) -> int:
        total = Bitvector[cls._o].type_byte_length() if cls._o > 0 else 0
        for (_, ftyp, fopt) in cls._field_indices.values():
            if fopt:
                continue
            if not ftyp.is_fixed_byte_length():
                total += OFFSET_BYTE_LENGTH
            total += ftyp.min_byte_length()
        return total

    @classmethod
    def max_byte_length(cls) -> int:
        total = Bitvector[cls._o].type_byte_length() if cls._o > 0 else 0
        for (_, ftyp, _) in cls._field_indices.values():
            if not ftyp.is_fixed_byte_length():
                total += OFFSET_BYTE_LENGTH
            total += ftyp.max_byte_length()
        return total

    @classmethod
    def is_packed(cls) -> bool:
        return False

    @classmethod
    def tree_depth(cls) -> int:
        return cls.B.tree_depth()

    @classmethod
    def item_elem_cls(cls, i: int) -> Type[View]:
        return cls.B.item_elem_cls(i)

    def active_fields(self) -> Bitvector:
        if not issubclass(self.__class__.B, StableContainer):
            raise Exception(f'`active_fields` requires `Profile` with `StableContainer` base')
        active_fields_node = super().get_backing().get_right()
        return Bitvector[self.__class__.B.N].view_from_backing(active_fields_node)

    def optional_fields(self) -> Bitvector:
        if not issubclass(self.__class__.B, StableContainer):
            raise Exception(f'`optional_fields` requires `Profile` with `StableContainer` base')
        if self.__class__._o == 0:
            raise Exception(f'`{self.__class__.__name__}` does not have any `Optional[T]` fields')
        active_fields = self.active_fields()
        optional_fields = Bitvector[self.__class__._o]()
        oindex = 0
        for (findex, _, fopt) in self.__class__._field_indices.values():
            if fopt:
                optional_fields.set(oindex, active_fields.get(findex))
                oindex += 1
        return optional_fields

    def __getattribute__(self, item):
        if item == 'B':
            raise AttributeError(f'Use `.__class__.{item}` to access `{item}`')
        return object.__getattribute__(self, item)

    def __getattr__(self, item):
        if item[0] == '_':
            return super().__getattribute__(item)
        else:
            try:
                (findex, ftyp, fopt) = self.__class__._field_indices[item]
            except KeyError:
                raise AttributeError(f'Unknown field `{item}`')

            if issubclass(self.__class__.B, StableContainer):
                value = stable_get(self, findex, ftyp, self.__class__.B.N)
            else:
                value = super().get(findex)
                if not isinstance(value, ftyp):
                    value = ftyp(backing=value.get_backing())
            assert value is not None or fopt
            return value

    def __setattr__(self, key, value):
        if key[0] == '_':
            super().__setattr__(key, value)
        else:
            try:
                (findex, ftyp, fopt) = self.__class__._field_indices[key]
            except KeyError:
                raise AttributeError(f'Unknown field `{key}`')

            if value is None and not fopt:
                raise ValueError(f'Field `{key}` is required and cannot be set to `None`')

            if issubclass(self.__class__.B, StableContainer):
                stable_set(self, findex, ftyp, self.__class__.B.N, value)
            else:
                super().set(findex, value)

    def __repr__(self):
        return f'{self.__class__.type_repr()}:\n' + '\n'.join(
            indent(field_val_repr(self, fkey, ftyp, fopt), '  ')
            for fkey, (_, ftyp, fopt) in self.__class__._field_indices.items())

    @classmethod
    def type_repr(cls) -> str:
        return f'Profile[{cls.B.__name__}]'

    @classmethod
    def deserialize(cls: Type[BV], stream: BinaryIO, scope: int) -> BV:
        if cls._o > 0:
            num_prefix_bytes = Bitvector[cls._o].type_byte_length()
            if scope < num_prefix_bytes:
                raise ValueError(f'Scope too small for `Profile[{cls.B.__name__}]` optional fields')
            optional_fields = Bitvector[cls._o].deserialize(stream, num_prefix_bytes)
            scope = scope - num_prefix_bytes

        field_values: Dict[str, Optional[View]] = {}
        dyn_fields: PyList[FieldOffset] = []
        fixed_size = 0
        oindex = 0
        for fkey, (_, ftyp, fopt) in cls._field_indices.items():
            if fopt:
                has_field = optional_fields.get(oindex)
                oindex += 1
                if not has_field:
                    field_values[fkey] = None
                    continue
            if ftyp.is_fixed_byte_length():
                fsize = ftyp.type_byte_length()
                field_values[fkey] = ftyp.deserialize(stream, fsize)
                fixed_size += fsize
            else:
                dyn_fields.append(FieldOffset(
                    key=fkey, typ=ftyp, offset=int(decode_offset(stream))))
                fixed_size += OFFSET_BYTE_LENGTH
        assert oindex == cls._o
        if len(dyn_fields) > 0:
            if dyn_fields[0].offset < fixed_size:
                raise Exception(f'First offset {dyn_fields[0].offset} is '
                                f'smaller than expected fixed size {fixed_size}')
            for i, (fkey, ftyp, foffset) in enumerate(dyn_fields):
                next_offset = dyn_fields[i + 1].offset if i + 1 < len(dyn_fields) else scope
                if foffset > next_offset:
                    raise Exception(f'Offset {i} is invalid: {foffset} '
                                    f'larger than next offset {next_offset}')
                fsize = next_offset - foffset
                f_min_size, f_max_size = ftyp.min_byte_length(), ftyp.max_byte_length()
                if not (f_min_size <= fsize <= f_max_size):
                    raise Exception(f'Offset {i} is invalid, size out of bounds: '
                                    f'{foffset}, next {next_offset}, implied size: {fsize}, '
                                    f'size bounds: [{f_min_size}, {f_max_size}]')
                field_values[fkey] = ftyp.deserialize(stream, fsize)
        return cls(**field_values)  # type: ignore

    def serialize(self, stream: BinaryIO) -> int:
        if self.__class__._o > 0:
            optional_fields = self.optional_fields()
            num_prefix_bytes = optional_fields.serialize(stream)
        else:
            num_prefix_bytes = 0

        num_data_bytes = 0
        has_dyn_fields = False
        oindex = 0
        for (_, ftyp, fopt) in self.__class__._field_indices.values():
            if fopt:
                has_field = optional_fields.get(oindex)
                oindex += 1
                if not has_field:
                    continue
            if ftyp.is_fixed_byte_length():
                num_data_bytes += ftyp.type_byte_length()
            else:
                num_data_bytes += OFFSET_BYTE_LENGTH
                has_dyn_fields = True
        assert oindex == self.__class__._o

        if has_dyn_fields:
            temp_dyn_stream = io.BytesIO()
        if issubclass(self.__class__.B, StableContainer):
            data = super().get_backing().get_left()
            active_fields = self.active_fields()
            n = self.__class__.B.N
        else:
            data = super().get_backing()
            n = len(self.__class__.B.fields())
        for (findex, ftyp, _) in self.__class__._field_indices.values():
            if issubclass(self.__class__.B, StableContainer) and not active_fields.get(findex):
                continue
            fnode = data.getter(2**get_depth(n) + findex)
            v = ftyp.view_from_backing(fnode)
            if ftyp.is_fixed_byte_length():
                v.serialize(stream)
            else:
                encode_offset(stream, num_data_bytes)
                num_data_bytes += v.serialize(temp_dyn_stream)  # type: ignore
        if has_dyn_fields:
            temp_dyn_stream.seek(0)
            stream.write(temp_dyn_stream.read(num_data_bytes))

        return num_prefix_bytes + num_data_bytes

    @classmethod
    def navigate_type(cls, key: Any) -> Type[View]:
        if key == '__active_fields__':
            return Bitvector[cls.B.N]
        (_, ftyp, fopt) = cls._field_indices[key]
        return Optional[ftyp] if fopt else ftyp

    @classmethod
    def key_to_static_gindex(cls, key: Any) -> Gindex:
        if key == '__active_fields__':
            return RIGHT_GINDEX
        (findex, _, _) = cls._field_indices[key]
        if issubclass(cls.B, StableContainer):
            return 2**get_depth(cls.B.N) * 2 + findex
        else:
            n = len(cls.B.fields())
            return 2**get_depth(n) + findex
