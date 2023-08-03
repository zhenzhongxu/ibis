from __future__ import annotations

import math
import numbers
import operator
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Hashable, Mapping, Sequence
from enum import Enum
from inspect import Parameter
from itertools import chain
from typing import Any as AnyType
from typing import (
    ForwardRef,
    Generic,  # noqa: F401
    Literal,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

import toolz
from typing_extensions import Annotated, GenericMeta, Self, get_args, get_origin

from ibis.common.bases import Singleton, Slotted
from ibis.common.collections import RewindableIterator, frozendict
from ibis.common.dispatch import lazy_singledispatch
from ibis.common.typing import Sentinel, get_bound_typevars, get_type_params
from ibis.util import is_iterable, promote_tuple

try:
    from types import UnionType
except ImportError:
    UnionType = object()


T_co = TypeVar("T_co", covariant=True)


class CoercionError(Exception):
    ...


class MatchError(Exception):
    ...


class Coercible(ABC):
    """Protocol for defining coercible types.

    Coercible types define a special ``__coerce__`` method that accepts an object
    with an instance of the type. Used in conjunction with the ``coerced_to``
    pattern to coerce arguments to a specific type.
    """

    __slots__ = ()

    @classmethod
    @abstractmethod
    def __coerce__(cls, value: Any, **kwargs: Any) -> Self:
        ...


class NoMatch(metaclass=Sentinel):
    """Marker to indicate that a pattern didn't match."""


# TODO(kszucs): have an As[int] or Coerced[int] type in ibis.common.typing which
# would be used to annotate an argument as coercible to int or to a certain type
# without needing for the type to inherit from Coercible
class Pattern(Hashable):
    """Base class for all patterns.

    Patterns are used to match values against a given condition. They are extensively
    used by other core components of Ibis to validate and/or coerce user inputs.
    """

    __slots__ = ()

    @classmethod
    def from_typehint(cls, annot: type, allow_coercion: bool = True) -> Pattern:
        """Construct a validator from a python type annotation.

        Parameters
        ----------
        annot
            The typehint annotation to construct the pattern from. This must be
            an already evaluated type annotation.
        allow_coercion
            Whether to use coercion if the typehint is a Coercible type.

        Returns
        -------
        pattern
            A pattern that matches the given type annotation.
        """
        # TODO(kszucs): cache the result of this function
        # TODO(kszucs): explore issubclass(typ, SupportsInt) etc.
        origin, args = get_origin(annot), get_args(annot)

        if origin is None:
            # the typehint is not generic
            if annot is Ellipsis or annot is AnyType:
                # treat both `Any` and `...` as wildcard
                return _any
            elif isinstance(annot, type):
                # the typehint is a concrete type (e.g. int, str, etc.)
                if allow_coercion and issubclass(annot, Coercible):
                    # the type implements the Coercible protocol so we try to
                    # coerce the value to the given type rather than checking
                    return CoercedTo(annot)
                else:
                    return InstanceOf(annot)
            elif isinstance(annot, TypeVar):
                # if the typehint is a type variable we try to construct a
                # validator from it only if it is covariant and has a bound
                if not annot.__covariant__:
                    raise NotImplementedError(
                        "Only covariant typevars are supported for now"
                    )
                if annot.__bound__:
                    return cls.from_typehint(annot.__bound__)
                else:
                    return _any
            elif isinstance(annot, Enum):
                # for enums we check the value against the enum values
                return EqualTo(annot)
            elif isinstance(annot, (str, ForwardRef)):
                # for strings and forward references we check in a lazy way
                return LazyInstanceOf(annot)
            else:
                raise TypeError(f"Cannot create validator from annotation {annot!r}")
        elif origin is Literal:
            # for literal types we check the value against the literal values
            return IsIn(args)
        elif origin is UnionType or origin is Union:
            # this is slightly more complicated because we need to handle
            # Optional[T] which is Union[T, None] and Union[T1, T2, ...]
            *rest, last = args
            if last is type(None):
                # the typehint is Optional[*rest] which is equivalent to
                # Union[*rest, None], so we construct an Option pattern
                if len(rest) == 1:
                    inner = cls.from_typehint(rest[0])
                else:
                    inner = AnyOf(*map(cls.from_typehint, rest))
                return Option(inner)
            else:
                # the typehint is Union[*args] so we construct an AnyOf pattern
                return AnyOf(*map(cls.from_typehint, args))
        elif origin is Annotated:
            # the Annotated typehint can be used to add extra validation logic
            # to the typehint, e.g. Annotated[int, Positive], the first argument
            # is used for isinstance checks, the rest are applied in conjunction
            annot, *extras = args
            return AllOf(cls.from_typehint(annot), *extras)
        elif origin is Callable:
            # the Callable typehint is used to annotate functions, e.g. the
            # following typehint annotates a function that takes two integers
            # and returns a string: Callable[[int, int], str]
            if args:
                # callable with args and return typehints construct a special
                # CallableWith validator
                arg_hints, return_hint = args
                arg_patterns = tuple(map(cls.from_typehint, arg_hints))
                return_pattern = cls.from_typehint(return_hint)
                return CallableWith(arg_patterns, return_pattern)
            else:
                # in case of Callable without args we check for the Callable
                # protocol only
                return InstanceOf(Callable)
        elif issubclass(origin, Tuple):
            # construct validators for the tuple elements, but need to treat
            # variadic tuples differently, e.g. tuple[int, ...] is a variadic
            # tuple of integers, while tuple[int] is a tuple with a single int
            first, *rest = args
            # TODO(kszucs): consider to support the same SequenceOf path if args
            # has a single element, e.g. tuple[int] since annotation a single
            # element tuple is not common OR use typing.Sequence for annotating
            # instead of tuple[T, ...] OR have a VarTupleOf pattern
            if rest == [Ellipsis]:
                inners = cls.from_typehint(first)
            else:
                inners = tuple(map(cls.from_typehint, args))
            return TupleOf(inners)
        elif issubclass(origin, Sequence):
            # construct a validator for the sequence elements where all elements
            # must be of the same type, e.g. Sequence[int] is a sequence of ints
            (value_inner,) = map(cls.from_typehint, args)
            return SequenceOf(value_inner, type=origin)
        elif issubclass(origin, Mapping):
            # construct a validator for the mapping keys and values, e.g.
            # Mapping[str, int] is a mapping with string keys and int values
            key_inner, value_inner = map(cls.from_typehint, args)
            return MappingOf(key_inner, value_inner, type=origin)
        elif isinstance(origin, GenericMeta):
            # construct a validator for the generic type, see the specific
            # Generic* validators for more details
            if allow_coercion and issubclass(origin, Coercible) and args:
                return GenericCoercedTo(annot)
            else:
                return GenericInstanceOf(annot)
        else:
            raise TypeError(
                f"Cannot create validator from annotation {annot!r} {origin!r}"
            )

    @abstractmethod
    def match(self, value: AnyType, context: dict[str, AnyType]) -> AnyType:
        """Match a value against the pattern.

        Parameters
        ----------
        value
            The value to match the pattern against.
        context
            A dictionary providing arbitrary context for the pattern matching.

        Returns
        -------
        match
            The result of the pattern matching. If the pattern doesn't match
            the value, then it must return the `NoMatch` sentinel value.
        """
        ...

    def is_match(self, value: AnyType, context: dict[str, AnyType]) -> bool:
        """Check if the value matches the pattern.

        Parameters
        ----------
        value
            The value to match the pattern against.
        context
            A dictionary providing arbitrary context for the pattern matching.

        Returns
        -------
        bool
            Whether the value matches the pattern.
        """
        return self.match(value, context) is not NoMatch

    @abstractmethod
    def __eq__(self, other: Pattern) -> bool:
        ...

    def __invert__(self) -> Not:
        """Syntax sugar for matching the inverse of the pattern."""
        return Not(self)

    def __or__(self, other: Pattern) -> AnyOf:
        """Syntax sugar for matching either of the patterns.

        Parameters
        ----------
        other
            The other pattern to match against.

        Returns
        -------
        New pattern that matches if either of the patterns match.
        """
        return AnyOf(self, other)

    def __and__(self, other: Pattern) -> AllOf:
        """Syntax sugar for matching both of the patterns.

        Parameters
        ----------
        other
            The other pattern to match against.

        Returns
        -------
        New pattern that matches if both of the patterns match.
        """
        return AllOf(self, other)

    def __rshift__(self, other: Builder) -> Replace:
        """Syntax sugar for replacing a value.

        Parameters
        ----------
        other
            The builder to use for constructing the replacement value.

        Returns
        -------
        New replace pattern.
        """
        return Replace(self, other)

    def __rmatmul__(self, name: str) -> Capture:
        """Syntax sugar for capturing a value.

        Parameters
        ----------
        name
            The name of the capture.

        Returns
        -------
        New capture pattern.
        """
        return Capture(name, self)


class Builder(Hashable):
    """A builder is a function that takes a context and returns a new object.

    The context is a dictionary that contains all the captured values and
    information relevant for the builder. The builder construct a new object
    only given by the context.

    The builder is used in the right hand side of the replace pattern:
    `Replace(pattern, builder)`. Semantically when a match occurs for the
    replace pattern, the builder is called with the context and the result
    of the builder is used as the replacement value.
    """

    __slots__ = ()

    @abstractmethod
    def __eq__(self, other):
        ...

    @abstractmethod
    def make(self, context: dict):
        """Construct a new object from the context.

        Parameters
        ----------
        context
            A dictionary containing all the captured values and information
            relevant for the builder.

        Returns
        -------
        The constructed object.
        """


def builder(obj):
    """Convert an object to a builder.

    It encapsulates:
        - callable objects into a `Factory` builder
        - non-callable objects into a `Just` builder

    Parameters
    ----------
    obj
        The object to convert to a builder.

    Returns
    -------
    The builder instance.
    """
    # TODO(kszucs): the replacer object must be handled differently from patterns
    # basically a replacer is just a lazy way to construct objects from the context
    # we should have a separate base class for replacers like Variable, Function,
    # Just, Apply and Call. Something like Replacer with a specific method e.g.
    # apply() could work
    if isinstance(obj, Builder):
        return obj
    elif callable(obj):
        # not function but something else
        return Factory(obj)
    else:
        return Just(obj)


class Variable(Slotted, Builder):
    """Retrieve a value from the context.

    Parameters
    ----------
    name
        The key to retrieve from the state.
    """

    __slots__ = ("name",)

    def __init__(self, name):
        super().__init__(name=name)

    def make(self, context):
        return context[self]

    def __getattr__(self, name):
        return Call(operator.attrgetter(name), self)

    def __getitem__(self, name):
        return Call(operator.itemgetter(name), self)


class Just(Slotted, Builder):
    """Construct exactly the given value.

    Parameters
    ----------
    value
        The value to return when the builder is called.
    """

    __slots__ = ("value",)

    def __init__(self, value):
        assert not isinstance(value, (Pattern, Builder))
        super().__init__(value=value)

    def make(self, context):
        return self.value


class Factory(Slotted, Builder):
    """Construct a value by calling a function.

    The function is called with two positional arguments:
    1. the value being matched
    2. the context dictionary

    The function must return the constructed value.

    Parameters
    ----------
    func
        The function to apply.
    """

    __slots__ = ("func",)

    def __init__(self, func):
        assert callable(func)
        super().__init__(func=func)

    def make(self, context):
        value = context[_]
        return self.func(value, context)


class Call(Slotted, Builder):
    """Pattern that calls a function with the given arguments.

    Both positional and keyword arguments are coerced into patterns.

    Parameters
    ----------
    func
        The function to call.
    args
        The positional argument patterns.
    kwargs
        The keyword argument patterns.
    """

    __slots__ = ("func", "args", "kwargs")

    def __init__(self, func, *args, **kwargs):
        assert callable(func)
        args = tuple(map(builder, args))
        kwargs = frozendict({k: builder(v) for k, v in kwargs.items()})
        super().__init__(func=func, args=args, kwargs=kwargs)

    def make(self, context):
        args = tuple(arg.make(context) for arg in self.args)
        kwargs = {k: v.make(context) for k, v in self.kwargs.items()}
        return self.func(*args, **kwargs)

    def __call__(self, *args, **kwargs):
        if self.args or self.kwargs:
            raise TypeError("Further specification of Call object is not allowed")
        return Call(self.func, *args, **kwargs)

    @classmethod
    def namespace(cls, module) -> Namespace:
        """Convenience method to create a namespace for easy object construction.

        Parameters
        ----------
        module
            The module object or name to look up the types.

        Examples
        --------
        >>> from ibis.common.patterns import Call
        >>> from ibis.expr.operations import Negate
        >>>
        >>> c = Call.namespace('ibis.expr.operations')
        >>> x = Variable('x')
        >>> pattern = c.Negate(x)
        >>> pattern
        Call(func=<class 'ibis.expr.operations.numeric.Negate'>, args=(Variable(name='x'),), kwargs=FrozenDict({}))
        >>> pattern.make({x: 5})
        <ibis.expr.operations.numeric.Negate object at 0x...>
        """
        return Namespace(cls, module)


# reserved variable name for the value being matched
_ = Variable("_")


class Always(Slotted, Singleton, Pattern):
    """Pattern that matches everything."""

    def match(self, value, context):
        return value


class Never(Slotted, Singleton, Pattern):
    """Pattern that matches nothing."""

    def match(self, value, context):
        return NoMatch


class Is(Slotted, Pattern):
    """Pattern that matches a value against a reference value.

    Parameters
    ----------
    value
        The reference value to match against.
    """

    __slots__ = ("value",)

    def match(self, value, context):
        if value is self.value:
            return value
        else:
            return NoMatch


class Any(Slotted, Singleton, Pattern):
    """Pattern that accepts any value, basically a no-op."""

    def match(self, value, context):
        return value


_any = Any()


class Capture(Slotted, Pattern):
    """Pattern that captures a value in the context.

    Parameters
    ----------
    pattern
        The pattern to match against.
    key
        The key to use in the context if the pattern matches.
    """

    __slots__ = ("key", "pattern")

    def __init__(self, key, pat=_any):
        super().__init__(key=key, pattern=pattern(pat))

    def match(self, value, context):
        value = self.pattern.match(value, context)
        if value is NoMatch:
            return NoMatch
        context[self.key] = value
        return value


class Replace(Slotted, Pattern):
    """Pattern that replaces a value with the output of another pattern.

    Parameters
    ----------
    matcher
        The pattern to match against.
    replacer
        The pattern to use as a replacement.
    """

    __slots__ = ("pattern", "builder")

    def __init__(self, matcher, replacer):
        super().__init__(pattern=pattern(matcher), builder=builder(replacer))

    def match(self, value, context):
        value = self.pattern.match(value, context)
        if value is NoMatch:
            return NoMatch
        # use the `_` reserved variable to record the value being replaced
        # in the context, so that it can be used in the replacer pattern
        context[_] = value
        return self.builder.make(context)


class Check(Slotted, Pattern):
    """Pattern that checks a value against a predicate.

    Parameters
    ----------
    predicate
        The predicate to use.
    """

    __slots__ = ("predicate",)

    def __init__(self, predicate):
        assert callable(predicate)
        super().__init__(predicate=predicate)

    def match(self, value, context):
        if self.predicate(value):
            return value
        else:
            return NoMatch


class Function(Slotted, Pattern):
    """Pattern that applies a function to the value.

    Parameters
    ----------
    func
        The function to apply.
    """

    __slots__ = ("func",)

    def __init__(self, func):
        assert callable(func)
        super().__init__(func=func)

    def match(self, value, context):
        return self.func(value, context)


class Namespace:
    """Convenience class for creating patterns for various types from a module.

    Useful to reduce boilerplate when creating patterns for various types from
    a module.

    Parameters
    ----------
    pattern
        The pattern to construct with the looked up types.
    module
        The module object or name to look up the types.

    Examples
    --------
    >>> from ibis.common.patterns import Namespace
    >>> import ibis.expr.operations as ops
    >>>
    >>> ns = Namespace(InstanceOf, ops)
    >>> ns.Negate
    InstanceOf(type=<class 'ibis.expr.operations.numeric.Negate'>)
    >>>
    >>> ns.Negate(5)
    Object(type=InstanceOf(type=<class 'ibis.expr.operations.numeric.Negate'>), args=(EqualTo(value=5),), kwargs=FrozenDict({}))
    """

    __slots__ = ("module", "pattern")

    def __init__(self, pattern, module):
        if isinstance(module, str):
            module = sys.modules[module]
        self.module = module
        self.pattern = pattern

    def __getattr__(self, name: str) -> Pattern:
        return self.pattern(getattr(self.module, name))


class Apply(Slotted, Pattern):
    """Pattern that applies a function to the value.

    The function must accept a single argument.

    Parameters
    ----------
    func
        The function to apply.

    Examples
    --------
    >>> from ibis.common.patterns import Apply, match
    >>>
    >>> match("a" @ Apply(lambda x: x + 1), 5)
    6
    """

    __slots__ = ("func",)

    def __init__(self, func):
        assert callable(func)
        super().__init__(func=func)

    def match(self, value, context):
        return self.func(value)

    def __call__(self, *args, **kwargs):
        """Convenience method to create a Call pattern."""
        return Call(self.func, *args, **kwargs)


class EqualTo(Slotted, Pattern):
    """Pattern that checks a value equals to the given value.

    Parameters
    ----------
    value
        The value to check against.
    """

    __slots__ = ("value",)

    def __init__(self, value):
        super().__init__(value=value)

    def match(self, value, context):
        if value == self.value:
            return value
        else:
            return NoMatch


class Option(Slotted, Pattern):
    """Pattern that matches `None` or a value that passes the inner validator.

    Parameters
    ----------
    pattern
        The inner pattern to use.
    """

    __slots__ = ("pattern", "default")

    def __init__(self, pat, default=None):
        super().__init__(pattern=pattern(pat), default=default)

    def match(self, value, context):
        if value is None:
            if self.default is None:
                return None
            else:
                return self.default
        else:
            return self.pattern.match(value, context)


class TypeOf(Slotted, Pattern):
    """Pattern that matches a value that is of a given type."""

    __slots__ = ("type",)

    def __init__(self, typ):
        super().__init__(type=typ)

    def match(self, value, context):
        if type(value) is self.type:
            return value
        else:
            return NoMatch


class SubclassOf(Slotted, Pattern):
    """Pattern that matches a value that is a subclass of a given type.

    Parameters
    ----------
    type
        The type to check against.
    """

    __slots__ = ("type",)

    def __init__(self, typ):
        super().__init__(type=typ)

    def match(self, value, context):
        if issubclass(value, self.type):
            return value
        else:
            return NoMatch


class InstanceOf(Slotted, Singleton, Pattern):
    """Pattern that matches a value that is an instance of a given type.

    Parameters
    ----------
    types
        The type to check against.
    """

    __slots__ = ("type",)

    def __init__(self, typ):
        super().__init__(type=typ)

    def match(self, value, context):
        if isinstance(value, self.type):
            return value
        else:
            return NoMatch

    def __call__(self, *args, **kwargs):
        return Object(self.type, *args, **kwargs)


class GenericInstanceOf(Slotted, Pattern):
    """Pattern that matches a value that is an instance of a given generic type.

    Parameters
    ----------
    typ
        The type to check against (must be a generic type).

    Examples
    --------
    >>> class MyNumber(Generic[T_co]):
    ...    value: T_co
    ...
    ...    def __init__(self, value: T_co):
    ...        self.value = value
    ...
    ...    def __eq__(self, other):
    ...        return type(self) is type(other) and self.value == other.value
    ...
    >>> p = GenericInstanceOf(MyNumber[int])
    >>> assert p.match(MyNumber(1), {}) == MyNumber(1)
    >>> assert p.match(MyNumber(1.0), {}) is NoMatch
    >>>
    >>> p = GenericInstanceOf(MyNumber[float])
    >>> assert p.match(MyNumber(1.0), {}) == MyNumber(1.0)
    >>> assert p.match(MyNumber(1), {}) is NoMatch
    """

    __slots__ = ("origin", "fields")

    def __init__(self, typ):
        origin = get_origin(typ)
        typevars = get_bound_typevars(typ)

        fields = {}
        for var, (attr, type_) in typevars.items():
            if not var.__covariant__:
                raise TypeError(
                    f"Typevar {var} is not covariant, cannot use it in a GenericInstanceOf"
                )
            fields[attr] = Pattern.from_typehint(type_, allow_coercion=False)

        super().__init__(origin=origin, fields=frozendict(fields))

    def match(self, value, context):
        if not isinstance(value, self.origin):
            return NoMatch

        for name, pattern in self.fields.items():
            attr = getattr(value, name)
            if pattern.match(attr, context) is NoMatch:
                return NoMatch

        return value


class LazyInstanceOf(Slotted, Pattern):
    """A version of `InstanceOf` that accepts qualnames instead of imported classes.

    Useful for delaying imports.

    Parameters
    ----------
    types
        The types to check against.
    """

    __slots__ = ("types", "check")

    def __init__(self, types):
        types = promote_tuple(types)
        check = lazy_singledispatch(lambda x: False)
        check.register(types, lambda x: True)
        super().__init__(types=types, check=check)

    def match(self, value, context):
        if self.check(value):
            return value
        else:
            return NoMatch


# TODO(kszucs): to support As[int] or CoercedTo[int] syntax
class CoercedTo(Slotted, Pattern):
    """Force a value to have a particular Python type.

    If a Coercible subclass is passed, the `__coerce__` method will be used to
    coerce the value. Otherwise, the type will be called with the value as the
    only argument.

    Parameters
    ----------
    type
        The type to coerce to.
    """

    __slots__ = ("target",)

    def __new__(cls, target):
        if issubclass(target, Coercible):
            return super().__new__(cls)
        else:
            return Apply(target)

    def __init__(self, target):
        assert isinstance(target, type)
        super().__init__(target=target)

    def match(self, value, context):
        try:
            value = self.target.__coerce__(value)
        except CoercionError:
            return NoMatch

        if isinstance(value, self.target):
            return value
        else:
            return NoMatch

    def __repr__(self):
        return f"CoercedTo({self.target.__name__!r})"


As = CoercedTo


class GenericCoercedTo(Slotted, Pattern):
    """Force a value to have a particular generic Python type.

    Parameters
    ----------
    typ
        The type to coerce to. Must be a generic type with bound typevars.

    Examples
    --------
    >>> from typing import Generic, TypeVar
    >>>
    >>> T = TypeVar("T", covariant=True)
    >>>
    >>> class MyNumber(Coercible, Generic[T]):
    ...     def __init__(self, value):
    ...         self.value = value
    ...
    ...     def __eq__(self, other):
    ...         return type(self) is type(other) and self.value == other.value
    ...
    ...     @classmethod
    ...     def __coerce__(cls, value, T=None):
    ...         if issubclass(T, int):
    ...             return cls(int(value))
    ...         elif issubclass(T, float):
    ...             return cls(float(value))
    ...         else:
    ...             raise CoercionError(f"Cannot coerce to {T}")
    ...
    >>> p = GenericCoercedTo(MyNumber[int])
    >>> assert p.match(3.14, {}) == MyNumber(3)
    >>> assert p.match("15", {}) == MyNumber(15)
    >>>
    >>> p = GenericCoercedTo(MyNumber[float])
    >>> assert p.match(3.14, {}) == MyNumber(3.14)
    >>> assert p.match("15", {}) == MyNumber(15.0)
    """

    __slots__ = ("origin", "params", "checker")

    def __init__(self, target):
        origin = get_origin(target)
        checker = GenericInstanceOf(target)
        params = frozendict(get_type_params(target))
        super().__init__(origin=origin, params=params, checker=checker)

    def match(self, value, context):
        try:
            value = self.origin.__coerce__(value, **self.params)
        except CoercionError:
            return NoMatch

        if self.checker.match(value, context) is NoMatch:
            return NoMatch

        return value


class Not(Slotted, Pattern):
    """Pattern that matches a value that does not match a given pattern.

    Parameters
    ----------
    pattern
        The pattern which the value should not match.
    """

    __slots__ = ("pattern",)

    def __init__(self, inner):
        super().__init__(pattern=pattern(inner))

    def match(self, value, context):
        if self.pattern.match(value, context) is NoMatch:
            return value
        else:
            return NoMatch


class AnyOf(Slotted, Pattern):
    """Pattern that if any of the given patterns match.

    Parameters
    ----------
    patterns
        The patterns to match against. The first pattern that matches will be
        returned.
    """

    __slots__ = ("patterns",)

    def __init__(self, *pats):
        patterns = tuple(map(pattern, pats))
        super().__init__(patterns=patterns)

    def match(self, value, context):
        for pattern in self.patterns:
            result = pattern.match(value, context)
            if result is not NoMatch:
                return result
        return NoMatch


class AllOf(Slotted, Pattern):
    """Pattern that matches if all of the given patterns match.

    Parameters
    ----------
    patterns
        The patterns to match against. The value will be passed through each
        pattern in order. The changes applied to the value propagate through the
        patterns.
    """

    __slots__ = ("patterns",)

    def __init__(self, *pats):
        patterns = tuple(map(pattern, pats))
        super().__init__(patterns=patterns)

    def match(self, value, context):
        for pattern in self.patterns:
            value = pattern.match(value, context)
            if value is NoMatch:
                return NoMatch
        return value


class Length(Slotted, Pattern):
    """Pattern that matches if the length of a value is within a given range.

    Parameters
    ----------
    exactly
        The exact length of the value. If specified, `at_least` and `at_most`
        must be None.
    at_least
        The minimum length of the value.
    at_most
        The maximum length of the value.
    """

    __slots__ = ("at_least", "at_most")

    def __init__(
        self,
        exactly: Optional[int] = None,
        at_least: Optional[int] = None,
        at_most: Optional[int] = None,
    ):
        if exactly is not None:
            if at_least is not None or at_most is not None:
                raise ValueError("Can't specify both exactly and at_least/at_most")
            at_least = exactly
            at_most = exactly
        super().__init__(at_least=at_least, at_most=at_most)

    def match(self, value, context):
        length = len(value)
        if self.at_least is not None and length < self.at_least:
            return NoMatch
        if self.at_most is not None and length > self.at_most:
            return NoMatch
        return value


class Contains(Slotted, Pattern):
    """Pattern that matches if a value contains a given value.

    Parameters
    ----------
    needle
        The item that the passed value should contain.
    """

    __slots__ = ("needle",)

    def __init__(self, needle):
        super().__init__(needle=needle)

    def match(self, value, context):
        if self.needle in value:
            return value
        else:
            return NoMatch


class IsIn(Slotted, Pattern):
    """Pattern that matches if a value is in a given set.

    Parameters
    ----------
    haystack
        The set of values that the passed value should be in.
    """

    __slots__ = ("haystack",)

    def __init__(self, haystack):
        super().__init__(haystack=frozenset(haystack))

    def match(self, value, context):
        if value in self.haystack:
            return value
        else:
            return NoMatch


In = IsIn


class SequenceOf(Slotted, Pattern):
    """Pattern that matches if all of the items in a sequence match a given pattern.

    Parameters
    ----------
    item
        The pattern to match against each item in the sequence.
    type
        The type to coerce the sequence to. Defaults to tuple.
    exactly
        The exact length of the sequence.
    at_least
        The minimum length of the sequence.
    at_most
        The maximum length of the sequence.
    """

    __slots__ = ("item", "type", "length")

    def __init__(
        self,
        item: Pattern,
        type: type = tuple,
        exactly: Optional[int] = None,
        at_least: Optional[int] = None,
        at_most: Optional[int] = None,
    ):
        item = pattern(item)
        type = CoercedTo(type)
        length = Length(at_least=at_least, at_most=at_most)
        super().__init__(item=item, type=type, length=length)

    def match(self, values, context):
        if not is_iterable(values):
            return NoMatch

        result = []
        for value in values:
            value = self.item.match(value, context)
            if value is NoMatch:
                return NoMatch
            result.append(value)

        result = self.type.match(result, context)
        if result is NoMatch:
            return NoMatch

        return self.length.match(result, context)


class TupleOf(Slotted, Pattern):
    """Pattern that matches if the respective items in a tuple match the given patterns.

    Parameters
    ----------
    fields
        The patterns to match the respective items in the tuple.
    """

    __slots__ = ("fields",)

    def __new__(cls, fields):
        if isinstance(fields, tuple):
            return super().__new__(cls)
        else:
            return SequenceOf(fields, tuple)

    def __init__(self, fields):
        fields = tuple(map(pattern, fields))
        super().__init__(fields=fields)

    def match(self, values, context):
        if not is_iterable(values):
            return NoMatch

        if len(values) != len(self.fields):
            return NoMatch

        result = []
        for pattern, value in zip(self.fields, values):
            value = pattern.match(value, context)
            if value is NoMatch:
                return NoMatch
            result.append(value)

        return tuple(result)


class MappingOf(Slotted, Pattern):
    """Pattern that matches if all of the keys and values match the given patterns.

    Parameters
    ----------
    key
        The pattern to match the keys against.
    value
        The pattern to match the values against.
    type
        The type to coerce the mapping to. Defaults to dict.
    """

    __slots__ = ("key", "value", "type")

    def __init__(self, key: Pattern, value: Pattern, type: type = dict):
        super().__init__(key=pattern(key), value=pattern(value), type=CoercedTo(type))

    def match(self, value, context):
        if not isinstance(value, Mapping):
            return NoMatch

        result = {}
        for k, v in value.items():
            if (k := self.key.match(k, context)) is NoMatch:
                return NoMatch
            if (v := self.value.match(v, context)) is NoMatch:
                return NoMatch
            result[k] = v

        result = self.type.match(result, context)
        if result is NoMatch:
            return NoMatch

        return result


class Attrs(Slotted, Pattern):
    __slots__ = ("fields",)

    def __init__(self, **fields):
        fields = frozendict(toolz.valmap(pattern, fields))
        super().__init__(fields=fields)

    def match(self, value, context):
        for attr, pattern in self.fields.items():
            if not hasattr(value, attr):
                return NoMatch

            v = getattr(value, attr)
            if match(pattern, v, context) is NoMatch:
                return NoMatch

        return value


class Object(Slotted, Pattern):
    """Pattern that matches if the object has the given attributes and they match the given patterns.

    The type must conform the structural pattern matching protocol, e.g. it must have a
    __match_args__ attribute that is a tuple of the names of the attributes to match.

    Parameters
    ----------
    type
        The type of the object.
    *args
        The positional arguments to match against the attributes of the object.
    **kwargs
        The keyword arguments to match against the attributes of the object.
    """

    __slots__ = ("type", "args", "kwargs")

    def __new__(cls, type, *args, **kwargs):
        if not args and not kwargs:
            return InstanceOf(type)
        else:
            return super().__new__(cls)

    def __init__(self, type, *args, **kwargs):
        type = pattern(type)
        args = tuple(map(pattern, args))
        kwargs = frozendict(toolz.valmap(pattern, kwargs))
        super().__init__(type=type, args=args, kwargs=kwargs)

    def match(self, value, context):
        if self.type.match(value, context) is NoMatch:
            return NoMatch

        patterns = {**self.kwargs, **dict(zip(value.__match_args__, self.args))}

        fields = {}
        changed = False
        for name, pattern in patterns.items():
            try:
                attr = getattr(value, name)
            except AttributeError:
                return NoMatch

            result = pattern.match(attr, context)
            if result is NoMatch:
                return NoMatch
            elif result != attr:
                changed = True
                fields[name] = result
            else:
                fields[name] = attr

        if changed:
            return type(value)(**fields)
        else:
            return value

    @classmethod
    def namespace(cls, module):
        return Namespace(InstanceOf, module)


class Node(Slotted, Pattern):
    __slots__ = ("type", "each_arg")

    def __init__(self, type, each_arg):
        super().__init__(type=pattern(type), each_arg=pattern(each_arg))

    def match(self, value, context):
        if self.type.match(value, context) is NoMatch:
            return NoMatch

        newargs = {}
        changed = False
        for name, arg in zip(value.__argnames__, value.__args__):
            result = self.each_arg.match(arg, context)
            if result is NoMatch:
                newargs[name] = arg
            else:
                newargs[name] = result
                changed = True

        if changed:
            return value.__class__(**newargs)
        else:
            return value


class CallableWith(Slotted, Pattern):
    __slots__ = ("args", "return_")

    def __init__(self, args, return_=_any):
        super().__init__(args=tuple(args), return_=return_)

    def match(self, value, context):
        from ibis.common.annotations import annotated

        if not callable(value):
            return NoMatch

        fn = annotated(self.args, self.return_, value)

        has_varargs = False
        positional, keyword_only = [], []
        for p in fn.__signature__.parameters.values():
            if p.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD):
                positional.append(p)
            elif p.kind is Parameter.KEYWORD_ONLY:
                keyword_only.append(p)
            elif p.kind is Parameter.VAR_POSITIONAL:
                has_varargs = True

        if keyword_only:
            raise MatchError(
                "Callable has mandatory keyword-only arguments which cannot be specified"
            )
        elif len(positional) > len(self.args):
            # Callable has more positional arguments than expected")
            return NoMatch
        elif len(positional) < len(self.args) and not has_varargs:
            # Callable has less positional arguments than expected")
            return NoMatch
        else:
            return fn


class PatternSequence(Slotted, Pattern):
    # TODO(kszucs): add a length optimization to not even try to match if the
    # length of the sequence is lower than the length of the pattern sequence

    __slots__ = ("pattern_window",)

    def __init__(self, patterns):
        current_patterns = [
            SequenceOf(_any) if p is Ellipsis else pattern(p) for p in patterns
        ]
        following_patterns = chain(current_patterns[1:], [Not(_any)])
        pattern_window = tuple(zip(current_patterns, following_patterns))
        super().__init__(pattern_window=pattern_window)

    def match(self, value, context):
        it = RewindableIterator(value)
        result = []

        if not self.pattern_window:
            try:
                next(it)
            except StopIteration:
                return result
            else:
                return NoMatch

        for current, following in self.pattern_window:
            original = current

            if isinstance(current, Capture):
                current = current.pattern
            if isinstance(following, Capture):
                following = following.pattern

            if isinstance(current, (SequenceOf, PatternSequence)):
                if isinstance(following, SequenceOf):
                    following = following.item
                elif isinstance(following, PatternSequence):
                    # first pattern to match from the pattern window
                    following = following.pattern_window[0][0]

                matches = []
                while True:
                    it.checkpoint()
                    try:
                        item = next(it)
                    except StopIteration:
                        break

                    res = following.match(item, context)
                    if res is NoMatch:
                        matches.append(item)
                    else:
                        it.rewind()
                        break

                res = original.match(matches, context)
                if res is NoMatch:
                    return NoMatch
                else:
                    result.extend(res)
            else:
                try:
                    item = next(it)
                except StopIteration:
                    return NoMatch

                res = original.match(item, context)
                if res is NoMatch:
                    return NoMatch
                else:
                    result.append(res)

        return result


class PatternMapping(Slotted, Pattern):
    __slots__ = ("keys", "values")

    def __init__(self, patterns):
        keys = PatternSequence(list(map(pattern, patterns.keys())))
        values = PatternSequence(list(map(pattern, patterns.values())))
        super().__init__(keys=keys, values=values)

    def match(self, value, context):
        if not isinstance(value, Mapping):
            return NoMatch

        keys = value.keys()
        if (keys := self.keys.match(keys, context)) is NoMatch:
            return NoMatch

        values = value.values()
        if (values := self.values.match(values, context)) is NoMatch:
            return NoMatch

        return dict(zip(keys, values))


class Between(Slotted, Pattern):
    """Match a value between two bounds.

    Parameters
    ----------
    lower
        The lower bound.
    upper
        The upper bound.
    """

    __slots__ = ("lower", "upper")

    def __init__(self, lower: float = -math.inf, upper: float = math.inf):
        super().__init__(lower=lower, upper=upper)

    def match(self, value, context):
        if self.lower <= value <= self.upper:
            return value
        else:
            return NoMatch


def NoneOf(*args) -> Pattern:
    """Match none of the passed patterns."""
    return Not(AnyOf(*args))


def ListOf(pattern):
    """Match a list of items matching the given pattern."""
    return SequenceOf(pattern, type=list)


def DictOf(key_pattern, value_pattern):
    """Match a dictionary with keys and values matching the given patterns."""
    return MappingOf(key_pattern, value_pattern, type=dict)


def FrozenDictOf(key_pattern, value_pattern):
    """Match a frozendict with keys and values matching the given patterns."""
    return MappingOf(key_pattern, value_pattern, type=frozendict)


def pattern(obj: AnyType) -> Pattern:
    """Create a pattern from various types.

    Parameters
    ----------
    obj
        The object to create a pattern from. Can be a pattern, a type, a callable,
        a mapping, an iterable or a value.

    Examples
    --------
    >>> assert pattern(Any()) == Any()
    >>> assert pattern(int) == InstanceOf(int)
    >>>
    >>> @pattern
    ... def as_int(x, context):
    ...     return int(x)
    >>>
    >>> assert as_int.match(1, {}) == 1

    Returns
    -------
    pattern
        The constructed pattern.
    """
    if obj is Ellipsis:
        return _any
    elif isinstance(obj, Pattern):
        return obj
    elif isinstance(obj, Mapping):
        return PatternMapping(obj)
    elif isinstance(obj, type):
        return InstanceOf(obj)
    elif get_origin(obj):
        return Pattern.from_typehint(obj)
    elif is_iterable(obj):
        return PatternSequence(obj)
    elif callable(obj):
        return Function(obj)
    else:
        return EqualTo(obj)


def match(
    pat: Pattern, value: AnyType, context: Optional[dict[str, AnyType]] = None
) -> Any:
    """Match a value against a pattern.

    Parameters
    ----------
    pat
        The pattern to match against.
    value
        The value to match.
    context
        Arbitrary mapping of values to be used while matching.

    Returns
    -------
    The matched value if the pattern matches, otherwise :obj:`NoMatch`.

    Examples
    --------
    >>> assert match(Any(), 1) == 1
    >>> assert match(1, 1) == 1
    >>> assert match(1, 2) is NoMatch
    >>> assert match(1, 1, context={"x": 1}) == 1
    >>> assert match(1, 2, context={"x": 1}) is NoMatch
    >>> assert match([1, int], [1, 2]) == [1, 2]
    >>> assert match([1, int, "a" @ InstanceOf(str)], [1, 2, "three"]) == [1, 2, "three"]
    """
    if context is None:
        context = {}

    pat = pattern(pat)
    result = pat.match(value, context)
    return NoMatch if result is NoMatch else result


class Topmost(Slotted, Pattern):
    """Traverse the value tree topmost first and match the first value that matches."""

    __slots__ = ("pattern", "filter")

    def __init__(self, searcher, filter=None):
        super().__init__(pattern=pattern(searcher), filter=filter)

    def match(self, value, context):
        result = self.pattern.match(value, context)
        if result is not NoMatch:
            return result

        for child in value.__children__(self.filter):
            result = self.match(child, context)
            if result is not NoMatch:
                return result

        return NoMatch


class Innermost(Slotted, Pattern):
    # matches items in the innermost layer first, but all matches belong to the same layer
    """Traverse the value tree innermost first and match the first value that matches."""

    __slots__ = ("pattern", "filter")

    def __init__(self, searcher, filter=None):
        super().__init__(pattern=pattern(searcher), filter=filter)

    def match(self, value, context):
        for child in value.__children__(self.filter):
            result = self.match(child, context)
            if result is not NoMatch:
                return result

        return self.pattern.match(value, context)


IsTruish = Check(lambda x: bool(x))
IsNumber = InstanceOf(numbers.Number) & ~InstanceOf(bool)
IsString = InstanceOf(str)