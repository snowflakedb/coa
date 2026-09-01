# Copyright 2026 Snowflake Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Generic, Optional, TypeAlias, TypeVar, overload

from eqgen.config.gcl_compat import TupleLike
from eqgen.config.weighted import Choices, Weighted

T = TypeVar("T")

# Gcl Tuple derived types.
GclTupleT = TypeVar("GclTupleT", bound="BaseGclTuple")

# A GCL tuple-like value can be either a plain dict (from ``util.to_python()``)
# or a raw GCL ``TupleLike`` object (e.g. ``CompositeTuple``).
_GclTupleLike: tuple[type, ...] = (Mapping, TupleLike)

# Enum derived types.
EnumT = TypeVar("EnumT", bound=Enum)

# Gcl supported plan old data (POD) types.
GclPodT = TypeVar("GclPodT", int, str, bool, float)

# Type of the dictionary form of GCL tuples from gcl.util.to_python.
GclDictT: TypeAlias = dict[str, Any]

# Return-type variable for typed descriptors.
_R = TypeVar("_R")


class RequiredKeyProperty(Generic[_R]):
    """Typed descriptor for required GCL key properties of classes derived from BaseGclTuple.

    Implements the descriptor protocol with ``__get__`` overloads so that mypy
    correctly infers the getter's return type (unlike a plain ``property``
    subclass, which mypy treats as returning ``Any``).

    See BaseGclTuple for more information.
    """

    def __init__(self, fget: Callable[..., _R]) -> None:
        self.fget = fget
        self.__doc__ = fget.__doc__

    @overload
    def __get__(self, obj: None, objtype: type) -> "RequiredKeyProperty[_R]": ...
    @overload
    def __get__(self, obj: Any, objtype: type) -> _R: ...
    def __get__(self, obj: Any, objtype: type | None = None) -> "_R | RequiredKeyProperty[_R]":
        if obj is None:
            return self
        return self.fget(obj)

    def __set__(self, obj: Any, value: Any) -> None:
        raise AttributeError("can't set attribute")


class OptionalKeyProperty(Generic[_R]):
    """Typed descriptor for optional GCL key properties of classes derived from BaseGclTuple.

    Implements the descriptor protocol with ``__get__`` overloads so that mypy
    correctly infers the getter's return type (unlike a plain ``property``
    subclass, which mypy treats as returning ``Any``).

    See BaseGclTuple for more information.
    """

    def __init__(self, fget: Callable[..., _R]) -> None:
        self.fget = fget
        self.__doc__ = fget.__doc__

    @overload
    def __get__(self, obj: None, objtype: type) -> "OptionalKeyProperty[_R]": ...
    @overload
    def __get__(self, obj: Any, objtype: type) -> _R: ...
    def __get__(self, obj: Any, objtype: type | None = None) -> "_R | OptionalKeyProperty[_R]":
        if obj is None:
            return self
        return self.fget(obj)

    def __set__(self, obj: Any, value: Any) -> None:
        raise AttributeError("can't set attribute")


def this_method_name() -> str:
    """
    Helper method to get the method name of the caller. Used in BaseGclTuple key properties to get the key name from the
    method name rather than copying it in a literal.
    :return: The caller's method name.
    """
    if (cf := inspect.currentframe()) is not None and (back := cf.f_back) is not None:
        return back.f_code.co_name
    raise RuntimeError("There is no stack in the _key() method")


def check_type(value: GclPodT, t: type[GclPodT]) -> GclPodT:
    assert isinstance(value, t), f"Value Class: {value.__class__} vs Required Class: {t}"
    return value


class BaseGclTuple:
    """
    Base class for accessing GCL Tuples through type checked and validated properties.

    GCL is built around named tuples, written with curly braces:
    {
      # This is a comment
      number: int = 1;
      string: string =  'value';  # Strings can be doubly-quoted as well
      bool: bool =  true;       # Note: lowercase
      expression: int = number * 2;
      list: [int] = [ 1, 2, 3 ];
      composed : ComposedFloatTuple = {
        float: float = 1.23;
      };
    }

    This class provides property based access to the keys in the tuple.

    This base class helps to solve several issues when working with GCL config files:
    1.) It adds type annotations to the properties and type checks at runtime.
    2.) It ensures all required keys from the BaseGclConfig are present in the GCL.
    3.) It ensures that no keys are present in the GCL without a corresponding property in the BaseGclTuple.
    4.) It provides property based access rather than dictionary access.

    It is used by creating lightweight derived classes for each tuple type in the GCL.
    For instance, if the GCL contains a tuple like:
    Thing: private = {
        required_thing: required int = 1;
        composed_tuple: required OtherThing = other_thing;
        composed_list: required [OtherThing] = [];
        enum_thing: str = 'Foo';
        optional_thing: bool = false;
    };
    A corresponding derived class should be created like:
    class Thing(BaseGclTuple):
        @RequiredKeyProperty
        def required_thing(self) -> int:
            return self._value_as(self._key(), int)

        @RequiredKeyProperty
        def composed_tuple(self) -> OtherThing: # Other thing is another BaseGclConfig derived class.
            return self._value_as_config(self._key(), OtherThing)

        @RequiredKeyProperty
        def composed_list(self) -> list[OtherThing]:
            return self._value_as_config_list(self._key(), OtherThing)

        @RequiredKeyProperty
        def enum_thing(self) -> MyEnum:
            return self._value_as_enum(self._key(), MyEnum)

        @OptionalKeyProperty
        def optional_thing(self) -> bool:
            return self._value_as(self._key(), bool)

    GCL POD(int, str, bool, float), tuples, lists of POD and tuples, as well as mappings of str -> POD/tuple are
        supported.

    The framework automatically checks that there is a RequiredKeyProperty or OptionalKeyProperty decorated getter with
    a name matching each key in a GCL tuple. If there isn't one present the framework will assert at runtime and the
    config validation test will fail.
    """

    def __init__(self, gcl_dict_tuple: GclDictT, validate_descendants: bool = True):
        """
        Gcl Tuple representing dict version of a GCL tuple from gcl.util.to_python().

        Checks that the types this tuple and all descendants match the types specified in the required and optional key
        properties.
        Checks that all required key properties are present.
        Checks that there are no keys without corresponding properties.

        :param gcl_dict_tuple: A dict version of a GCL tuple from gcl.util.to_python().
        :param validate_descendants: If true, descendant types should be validated during initialization. This is set to
            false for children that already were validated by a parent.
        """
        self._all_keys = self.__required_keys().union(self.__optional_keys())
        self.__assert_keys_match(gcl_dict_tuple)
        self._gcl_tuple = gcl_dict_tuple
        # Visiting descendants with the no-op visitor will still invoke type checking which will catch type issues early
        if validate_descendants:
            self.accept(GclTupleVisitor())

    def __repr__(self) -> str:
        """
        :return: Text version of the original dict Gcl Tuple provided to the constructor.
        """
        return str(self._gcl_tuple)

    _required_keys_cache: ClassVar[dict[type, set[str]]] = {}
    _optional_keys_cache: ClassVar[dict[type, set[str]]] = {}

    def __required_keys(self) -> set[str]:
        """
        This method returns the names of the RequiredKeyProperty decorated properties in a derived class. These are the
        keys which must be present in the provided GCL tuple.

        :return: The set of required keys specified in the derived class.
        """
        cls = type(self)
        if cls not in BaseGclTuple._required_keys_cache:
            BaseGclTuple._required_keys_cache[cls] = {
                p[0] for p in inspect.getmembers(cls, lambda o: isinstance(o, RequiredKeyProperty))
            }
        return BaseGclTuple._required_keys_cache[cls]

    def __optional_keys(self) -> set[str]:
        """
        This method returns the names of the OptionalKeyProperty decorated properties in a derived class. These are the
        keys which could be present in the provided GCL tuple but are not required.

        :return: The set of optional keys specified in the derived class.
        """
        cls = type(self)
        if cls not in BaseGclTuple._optional_keys_cache:
            BaseGclTuple._optional_keys_cache[cls] = {
                p[0] for p in inspect.getmembers(cls, lambda o: isinstance(o, OptionalKeyProperty))
            }
        return BaseGclTuple._optional_keys_cache[cls]

    def __assert_keys_match(self, gcl_dict_tuple: GclDictT) -> None:
        """
        Asserts that all required keys are present the provided GCL dict tuple and that no keys are present that do not
        have corresponding key properties defined.

        :param gcl_dict_tuple: A dict version of a GCL tuple from gcl.util.to_python().
        :return: None
        """
        missing_gcl_keys = self.__required_keys().difference(gcl_dict_tuple.keys())
        missing_config_keys = set(gcl_dict_tuple.keys()).difference(self._all_keys)
        cls_name = self.__class__.__name__
        assert len(missing_gcl_keys) == 0 and len(missing_config_keys) == 0, (
            f"Keys in {cls_name} missing in GCL: {missing_gcl_keys}\n"
            f"Keys in GCL missing in {cls_name}: {missing_config_keys}\n"
            f"{cls_name} keys required: {self.__required_keys()} optional: {self.__optional_keys()}\n"
            f"GCL: {gcl_dict_tuple}."
        )
        if len(missing_gcl_keys) > 0 or len(missing_config_keys) > 0:
            raise ValueError(
                f"Keys in {cls_name} missing in GCL: {missing_gcl_keys}\n"
                f"Keys in GCL missing in {cls_name}: {missing_config_keys}\n"
                f"{cls_name} keys required: {self.__required_keys()} optional: {self.__optional_keys()}\n"
                f"GCL: {gcl_dict_tuple}."
            )

    def __assert_type(self, key: str, t: type | tuple[type, ...], optional: bool = False) -> None:
        """
        Asserts that an optional key is either not present or that its value has a matching type.

        :param key: Optional property key
        :param t: The type to assert that the corresponding value matches if present.
        :param optional: If true, optional key, otherwise required
        :return: None
        """
        if optional and not self._optional_key_present(key):
            # Nothing to check since there is no value.
            return
        r = self.__value_as_optional_any(key) if optional else self.__value_as_any(key)
        assert isinstance(r, t), f"Value Class: {r.__class__} vs Required Class: {t}\n\n{key}: {r}"

    def __assert_element_type(self, key: str, elem_t: type | tuple[type, ...], optional: bool = False) -> None:
        """
        Asserts that a required key is present and that its value has a matching type.

        :param key: Required property key
        :param t: The type to assert that the corresponding value matches.
        :return: None
        """
        if optional and not self._optional_key_present(key):
            # Nothing to check since there is no value.
            return
        r = self.__value_as_optional_any(key) if optional else self.__value_as_any(key)
        t = Sequence
        assert isinstance(r, t), f"Value Class: {r.__class__} vs Required Class: {t}\n\n{key}: {r}"
        for idx, elem in enumerate(r):
            assert isinstance(elem, elem_t), f"Value Class: {elem.__class__} vs Required Class: {elem_t}\n\n{idx}: {elem}"

    def __assert_value_type(self, key: str, value_t: type | tuple[type, ...], optional: bool = False) -> None:
        """
        Asserts that a required key is present and that its value has a matching type.

        :param key: Required property key
        :param t: The type to assert that the corresponding value matches.
        :return: None
        """
        if optional and not self._optional_key_present(key):
            # Nothing to check since there is no value.
            return
        r = self.__value_as_optional_any(key) if optional else self.__value_as_any(key)
        t = _GclTupleLike
        assert isinstance(r, t), f"Value Class: {r.__class__} vs Required Class: {t}\n\n{key}: {r}"
        for key, value in r.items():  # type: ignore[attr-defined]
            assert isinstance(value, value_t), f"Value Class: {value.__class__} vs Required Class: {value_t}\n\n{key}: {value}"

    def _optional_key_present(self, key: str) -> bool:
        assert key in self.__optional_keys(), f"{key} in {self.__optional_keys()}"
        return key in self._gcl_tuple

    def __value_as_optional_any(self, key: str) -> Optional[Any]:
        """
        Gets the value corresponding to the optional property key or None if it's not present.
        Asserts that the key is a valid optional key.

        :param key: Optional property key
        :return: The value of the property or None if not present
        """
        assert key in self.__optional_keys(), f"{key} in {self.__optional_keys()}"
        if key not in self._gcl_tuple:
            return None
        return self._gcl_tuple[key]

    def _value_as_optional(self, key: str, t: type[GclPodT]) -> Optional[GclPodT]:
        """
        Gets the value corresponding to the optional property key as a POD value of the specified type or None if the
        key is not present.

        :param key: Optional property key
        :param t: POD type (int, float, str, bool) of the value if present
        :return: t typed value or None if key is not present
        """
        self.__assert_type(key, t, True)
        if (optional_value := self.__value_as_optional_any(key)) is not None:
            return t(optional_value)
        return None

    def _value_as_optional_gcl_tuple(self, key: str, t: type[GclTupleT]) -> Optional[GclTupleT]:
        """
        Gets the value corresponding to the optional property key as a BaseGclTuple derived value of the specified type
        or None if the key is not present.

        :param key: Optional property key
        :param t: GclTupleBase derived type
        :return: t typed value or None if key is not present
        """
        if (optional_value := self.__value_as_optional_any(key)) is not None:
            self.__assert_type(key, _GclTupleLike, True)
            return t(optional_value, False)
        return None

    def _value_as_optional_gcl_tuple_list(self, key: str, elem_t: type[GclTupleT]) -> Optional[Sequence[GclTupleT]]:
        """
        Gets the value corresponding to the optional property key as a list of Gcl Tuple typed elements or None if the
        key is not present.

        :param key: Optional property key
        :param elem_t: GclTupleBase derived element type
        :return: elem_t typed sequence of values or None if key is not present
        """
        self.__assert_element_type(key, _GclTupleLike, True)
        if (optional_value := self.__value_as_optional_any(key)) is not None:
            return [elem_t(i, False) for i in optional_value]
        return None

    def __value_as_any(self, key: str) -> Any:
        """
        Gets the value corresponding to the required property key.
        Asserts that the key is a valid required key.

        :param key: Required property key
        :return: The value of the property
        """
        assert key in self.__required_keys(), f"{key} in {self.__required_keys()}"
        return self._gcl_tuple[key]

    def _value_as(self, key: str, t: type[GclPodT]) -> GclPodT:
        """
        Gets the value corresponding to the required property key as a POD (int, float, str, bool) type.

        :param key: Required property key
        :param t: POD type (int, float, str, bool) of the value
        :return: t typed value
        """
        self.__assert_type(key, t)
        return t(self.__value_as_any(key))

    def _value_as_enum(self, key: str, t: type[EnumT]) -> EnumT:
        """
        Gets the value corresponding to the required property key as a Enum derived type.
        Will raise KeyError if the value doesn't match any names of t.

        :param key: Required property key
        :param t: Enum derived type which has values matching the domain of the GCL dict names
        :return: t typed value
        """
        v = self.__value_as_any(key)
        return t.__getitem__(v)

    def _value_as_list(self, key: str, elem_t: type[GclPodT]) -> Sequence[GclPodT]:
        """
        Gets the value corresponding to the required property key as a list of POD (int, float, str, bool) typed
        elements.
        Asserts that the value is a list.

        :param key: Required property key
        :param elem_t: POD type (int, float, str, bool) of the element value
        :return: sequence of values of type elem_t
        """
        self.__assert_element_type(key, elem_t)
        for e in self.__value_as_any(key):
            assert isinstance(e, elem_t), f"Elements must be type {elem_t} but is {type(e)}. {key}: {e}. {self}"
        return [elem_t(i) for i in self.__value_as_any(key)]

    def _value_as_dict(self, key: str, value_t: type[GclPodT]) -> Mapping[str, GclPodT]:
        """
        Gets the value corresponding to the required property key as a mapping of string keys to a specified POD data
        type.
        Assets that the value is a mapping.

        :param key: Required property key
        :param value_t: POD type (int, float, str, bool) of the mapping value
        :return: A mapping of string keys to elements of type value_t
        """
        self.__assert_value_type(key, value_t)
        for k, v in self.__value_as_any(key).items():
            assert isinstance(v, value_t), f"Value must be type {value_t} but is {type(v)}. {key}: {k}: {v}. {self}"
            assert isinstance(k, str), f"Key must be type {str} but is {type(k)}. {key}: {k}: {v}. {self}"
        return MappingProxyType[str, GclPodT]({k: value_t(v) for (k, v) in self.__value_as_any(key).items()})

    def _value_as_choices(self, key: str, t: type[T]) -> Choices[T]:
        """
        Gets the value corresponding to the required property key as a choice of weighted POD values.
        The value must be a sequence of Weighted mappings with a 'v' value of type t and a 'weight' value of type int.
        Assets that the value is a mapping.

        :param key: Required property key
        :param t: POD type (int, float, str, bool) of the choice value
        :return: Choices class with values of type t
        """
        self.__assert_element_type(key, _GclTupleLike)
        for c in self.__value_as_any(key):
            assert isinstance(c["v"], t), f"Value must be type {t} but is {type(c['v'])}. {key}: {c}. {self}"
            w = c.get("weight", 1) if hasattr(c, "get") else c["weight"]
            assert isinstance(w, int), f"Weight must be type {int} but is {type(w)}. {key}: {c}. {self}"
        return Choices(
            [Weighted(c["v"], c.get("weight", 1) if hasattr(c, "get") else c["weight"], t) for c in self.__value_as_any(key)], t
        )

    def _value_as_choices_enum(self, key: str, t: type[EnumT]) -> Choices[EnumT]:
        """Gets the required property as weighted choices of an Enum type.

        GCL string values are resolved to enum members by **name**
        (e.g. ``'INNER'`` -> ``JoinType.INNER``).

        :param key: Required property key
        :param t: Enum subclass whose member names match the GCL strings
        :return: Choices with enum-typed values
        """
        self.__assert_element_type(key, _GclTupleLike)
        for c in self.__value_as_any(key):
            assert isinstance(c["v"], str), f"Value must be str but is {type(c['v'])}. {key}: {c}. {self}"
            assert c["v"] in t.__members__, f"Value {c['v']!r} is not a member of {t.__name__}. {key}: {c}. {self}"
            assert isinstance(c["weight"], int), f"Weight must be type {int} but is {type(c['weight'])}. {key}: {c}. {self}"
        # Look up by member name via ``__members__`` (not ``t.__getitem__``): for a
        # ``StrEnum`` (a ``str`` subclass) ``t.__getitem__`` resolves to ``str``'s
        # element indexing, not the ``EnumMeta`` name lookup.  ``__members__`` works for
        # both plain ``Enum`` and ``StrEnum``.
        return Choices([Weighted(t.__members__[c["v"]], c["weight"], t) for c in self.__value_as_any(key)], t)

    @staticmethod
    def _filter_private_keys(raw: Any) -> Any:
        """Filter private GCL keys from a ``TupleLike`` value.

        When a GCL file is ``include``-d from another file, private keys
        (e.g. ``Value: private = include 'value.gcl'``) are still present in
        the raw ``TupleLike`` object.  ``exportable_keys()`` returns only the
        public keys.  Plain dicts (from ``util.to_python()``) are returned
        unchanged.
        """
        if isinstance(raw, TupleLike):
            return {k: raw[k] for k in raw.exportable_keys()}
        return raw

    def _value_as_gcl_tuple(self, key: str, t: type[GclTupleT]) -> GclTupleT:
        """
        Gets the value corresponding to the required property key as a Gcl Tuple of type t.
        Assets that the value is a mapping.

        :param key: Required property key
        :param t: BaseGclTuple derived class type
        :return: Value as type t
        """
        self.__assert_type(key, _GclTupleLike)
        return t(self._filter_private_keys(self.__value_as_any(key)), False)

    def _value_as_gcl_tuple_list(self, key: str, t: type[GclTupleT]) -> Sequence[GclTupleT]:
        """
        Gets the value corresponding to the required property key as a sequence of Gcl Tuple of type t.
        Assets that each element value is a mapping.

        :param key: Required property key
        :param t: BaseGclTuple derived class type of each element of the sequence
        :return: Value as sequence with elements of type t
        """
        self.__assert_element_type(key, _GclTupleLike)
        return [t(i, False) for i in self.__value_as_any(key)]

    def _value_as_gcl_tuple_dict(self, key: str, t: type[GclTupleT]) -> Mapping[str, GclTupleT]:
        """
        Gets the value corresponding to the required property key as a mapping of each string key to the corresponding
         Gcl Tuple of type t.
        Assets that each element value is a mapping.

        :param key: Required property key
        :param t: BaseGclTuple derived class type of each value in the mapping
        :return: Value as mapping of string keys to corresponding values of type t
        """
        self.__assert_value_type(key, _GclTupleLike)
        return MappingProxyType[str, GclTupleT]({k: t(v, False) for k, v in self.__value_as_any(key).items()})

    def _value_as_gcl_tuple_choices(self, key: str, t: type[GclTupleT]) -> Choices[GclTupleT]:
        """
        Gets the value corresponding to the required property key as a choice of weighted Gcl Tuple values.
        The value must be a sequence of Weighted mappings with a 'v' value of type t and a 'weight' value of type int.
        Assets that the value is a mapping.

        :param key: Required property key
        :param t: BaseGclTuple derived class type of each weighted value
        :return: Choices class with values of type t
        """
        self.__assert_element_type(key, _GclTupleLike)
        return Choices[GclTupleT]([Weighted[GclTupleT](t(c["v"], False), c["weight"], t) for c in self.__value_as_any(key)], t)

    def _value_as_dispatched_gcl_tuple(self, key: str, discriminator: str, resolve: Callable[[str], type[GclTupleT]]) -> GclTupleT:
        """
        Read a GCL tuple field and dispatch to the correct class based on a discriminator field.

        :param key: Required property key
        :param discriminator: The key within the sub-tuple that determines which class to use
        :param resolve: Callable that maps a discriminator string to the concrete GclTuple class.
            Should raise ``ValueError`` for unrecognised discriminator values.
        :return: The dispatched GclTuple value
        """
        self.__assert_type(key, _GclTupleLike)
        raw = self.__value_as_any(key)
        disc_value = raw[discriminator]
        cls = resolve(disc_value)
        return cls(raw, False)

    def accept(self, visitor: "GclTupleVisitor") -> None:
        """
        Accept a visitor which will visit each value in this tuple and in each child tuple.

        :param visitor: Visitor to call at each value in this Gcl tuple and any child Gcl tuples.
        :return: None
        """
        with VisitationPath([], self.__class__.__name__) as child:
            self._accept_any(child, visitor, self)

    @staticmethod
    def _accept_sequence_elements(cur: "VisitationPath", visitor: "GclTupleVisitor", v: Sequence[Any]) -> None:
        """
        Accept each element in a sequence by calling _accept_any with the provided visitor.

        :param cur: Current path
        :param visitor: Visitor to use
        :param v: The sequence to accept the visitor
        :return: None
        """
        for idx, c in enumerate(v):
            with VisitationPath(cur.path, str(idx)) as child:
                BaseGclTuple._accept_any(child, visitor, c)

    @staticmethod
    def _accept_mapping_values(cur: "VisitationPath", visitor: "GclTupleVisitor", v: Mapping[str, Any]) -> None:
        """
        Accept each value in a mapping by calling _accept_any with the provided visitor.

        :param cur: Current path
        :param visitor: Visitor to use
        :param v: The mapping to accept the visitor
        :return: None
        """
        for key, c in v.items():
            with VisitationPath(cur.path, key) as child:
                BaseGclTuple._accept_any(child, visitor, c)

    @staticmethod
    def _accept_gcl_tuple_keys(cur: "VisitationPath", visitor: "GclTupleVisitor", v: GclTupleT) -> None:
        """
        Accept each required and optional key in the tuple.

        :param cur: Current path
        :param visitor: Visitor to use
        :param v: The Gcl Tuple to visit
        :return: None
        """
        for rkp in inspect.getmembers(type(v), lambda o: isinstance(o, RequiredKeyProperty)):
            with VisitationPath(cur.path, rkp[0]) as child:
                assert hasattr(v, rkp[0]), f"Required attribute {rkp[0]} at path {child} should be present in {v}"
                assert getattr(v, rkp[0]) is not None, f"Required attribute {rkp[0]} at path {child} must not be None in {v}"
                BaseGclTuple._accept_any(child, visitor, getattr(v, rkp[0]))
        for rkp in inspect.getmembers(type(v), lambda o: isinstance(o, OptionalKeyProperty)):
            with VisitationPath(cur.path, rkp[0]) as child:
                assert hasattr(v, rkp[0]), f"Optional attribute {rkp[0]} at path {child} should be present in {v}"
                BaseGclTuple._accept_any(child, visitor, getattr(v, rkp[0]))

    @staticmethod
    def _accept_any(cur: "VisitationPath", visitor: "GclTupleVisitor", v: Any) -> None:
        """
        Visit the current value and then accept any children.

        :param cur: Current path
        :param visitor: Visitor to use
        :param v: Value being visited
        :return: None
        """
        if isinstance(v, BaseGclTuple):
            visitor.visit_gcl_tuple(cur, v)
            BaseGclTuple._accept_gcl_tuple_keys(cur, visitor, v)
        elif isinstance(v, list):
            # TODO: Find a better way to get runtime type info here.
            if len(v) > 0 and isinstance(v[0], BaseGclTuple):
                visitor.visit_gcl_tuple_sequence(cur, v)
            else:
                visitor.visit_sequence(cur, v)
            BaseGclTuple._accept_sequence_elements(cur, visitor, v)
        elif isinstance(v, Mapping):
            visitor.visit_mapping(cur, v)
            BaseGclTuple._accept_mapping_values(cur, visitor, v)
        elif isinstance(v, Choices):
            if isinstance(v.data_type, type) and issubclass(v.data_type, BaseGclTuple):
                visitor.visit_gcl_tuple_choices(cur, v)
            else:
                visitor.visit_choices(cur, v)
            BaseGclTuple._accept_any(cur, visitor, v.values)
        elif isinstance(v, Enum):
            visitor.visit_enum(cur, v)
        elif v is None:
            visitor.visit_missing_optional(cur)
        else:
            visitor.visit(cur, v)


class VisitationPath:
    """
    The path taken to the current visit by a GclTupleVisitor.

    This class helps maintain a list of string entries representing the path by implementing scoped path extension and
    asserting that the scope is maintained.

    It should be used in a with block:
    with VisitationPath(cur.path, 'new_entry') as child:
        do some child things...
    """

    def __init__(self, path: list[str], entry: str):
        """
        Add a new entry to the path list scoped by a with block for this class.

        :param path: The path to extend
        :param entry: The new entry to add
        """
        self._path: list[str] = path
        self._entry: str = entry

    @property
    def path(self) -> list[str]:
        """
        :return: The full path list.
        """
        assert self._path[-1] == self._entry
        return self._path

    @property
    def entry(self) -> str:
        """

        :return: The entry
        """
        return self._entry

    def __str__(self) -> str:
        """
        :return: A string representation of the path for human debugging.
        """
        return "\\".join(self._path)

    def __enter__(self) -> "VisitationPath":
        """
        Add entry to path.

        :return: self
        """
        self._path.append(self._entry)
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
        """
        Remove entry from path.

        :param exc_type: Not used
        :param exc_value: Not used
        :param exc_traceback: Not used
        :return: None
        """
        top: str = self._path.pop()
        assert self._entry == top, f"Path {self} had unexpected top {top} rather than {self._entry}"


class GclTupleVisitor:
    """
    Visitor for BaseGclTuple derived classes.

    Used by calling gcl_tuple_class.accept(my_visitor) after implementing your own derived visitor class.

    Each value in the Gcl Tuple will be visited, followed by visiting its children recursively.
    """

    def visit(self, path: VisitationPath, v: GclPodT) -> None:
        """
        Visit a POD value.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_enum(self, path: VisitationPath, v: EnumT) -> None:
        """
        Visit an Enum value.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_mapping(self, path: VisitationPath, v: Mapping[str, GclPodT]) -> None:
        """
        Visit a mapping value from string keys to POD values of a given type.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_sequence(self, path: VisitationPath, v: Sequence[GclPodT]) -> None:
        """
        Visit a sequence value with elements of a given type.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_gcl_tuple(self, path: VisitationPath, v: GclTupleT) -> None:
        """
        Visit a BaseGclTuple derived class of a given type.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_gcl_tuple_sequence(self, path: VisitationPath, v: Sequence[GclTupleT]) -> None:
        """
        Visit a sequence with BaseGclTuple derived class elements.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_choices(self, path: VisitationPath, v: Choices[GclPodT]) -> None:
        """
        Visit a Choices class with the given POD type values.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_gcl_tuple_choices(self, path: VisitationPath, v: Choices[GclTupleT]) -> None:
        """
        Visit a Choices class with the given BaseGclTuple derived type values.

        :param path: Current visitation path
        :param v: The value being visited
        :return: None
        """
        pass

    def visit_missing_optional(self, path: VisitationPath) -> None:
        """
        Visit a missing optional key

        :param path: Current visitation path
        :return: None
        """
        pass
