#  Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
#  See https://llvm.org/LICENSE.txt for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, List, Optional, Set, Tuple, Dict

from .common import (AppID, Clock, Input, Output, PortError, _PyProxy, Reset)
from .support import (get_user_loc, _obj_to_attribute, create_type_string,
                      create_const_zero)
from .signals import ClockSignal, Signal, _FromCirctValue
from .types import ClockType, Type

from .circt import ir, support
from .circt.dialects import hw
from .circt.support import BackedgeBuilder, attribute_to_var

import builtins
from contextvars import ContextVar
import inspect
import os
import sys

# A memoization table for module parameterization function calls.
_MODULE_CACHE: Dict[Tuple[builtins.function, ir.DictAttr], object] = {}


def _create_module_name(name: str, params: ir.DictAttr):
  """Create a "reasonable" module name from a base name and a set of
  parameters. E.g. PolyComputeForCoeff_62_42_6."""

  def val_str(val):
    if isinstance(val, ir.Type):
      return create_type_string(val)
    if isinstance(val, ir.Attribute):
      return str(attribute_to_var(val))
    return str(val)

  param_strings = []
  for p in params:
    param_strings.append(p.name + val_str(p.attr))
  for ps in sorted(param_strings):
    name += "_" + ps

  ret = ""
  name = name.replace("!hw.", "")
  for c in name:
    if c.isalnum():
      ret = ret + c
    elif c not in "!>[],\"" and len(ret) > 0 and ret[-1] != "_":
      ret = ret + "_"
  return ret.strip("_")


def _get_module_cache_key(func,
                          params) -> Tuple[builtins.function, ir.DictAttr]:
  """The "module" cache is specifically for parameterized modules. It maps the
  module parameterization function AND parameter values to the class which was
  generated by a previous call to said module parameterization function."""
  if not isinstance(params, ir.DictAttr):
    params = _obj_to_attribute(params)
  return (func, params)


_current_block_context = ContextVar("current_block_context")


class _BlockContext:
  """Bookkeeping for a generator scope."""

  def __init__(self):
    self.symbols: set[str] = set()

  @staticmethod
  def current() -> _BlockContext:
    """Get the top-most context in the stack created by `with
    _BlockContext()`."""
    bb = _current_block_context.get(None)
    assert bb is not None
    return bb

  def __enter__(self):
    self._old_system_token = _current_block_context.set(self)

  def __exit__(self, exc_type, exc_value, traceback):
    if exc_value is not None:
      return
    _current_block_context.reset(self._old_system_token)

  def uniquify_symbol(self, sym: str) -> str:
    """Create a unique symbol and add it to the cache. If it is to be preserved,
    the caller must use it as the symbol on a top-level op."""
    ctr = 0
    ret = sym
    while ret in self.symbols:
      ctr += 1
      ret = f"{sym}_{ctr}"
    self.symbols.add(ret)
    return ret


class Generator:
  """
  Represents a generator. Stores the generate function and location of
  the generate call. Generator objects are passed to module-specific generator
  object handlers.
  """

  def __init__(self, gen_func):
    self.gen_func = gen_func
    self.loc = get_user_loc()


def generator(func):
  """Decorator for generation functions."""
  return Generator(func)


class PortProxyBase:
  """Extensions of this class provide access to module ports in generators.
  Subclasses essentially just provide syntactic sugar around the methods in this
  base class. None of the methods here are intended to be directly used by the
  PyCDE developer."""

  def __init__(self, block_args, builder):
    self._block_args = block_args
    if builder.outputs is not None:
      self._output_values = [None] * len(builder.outputs)
    self._builder = builder

  def _get_input(self, idx):
    val = self._block_args[idx]
    if idx in self._builder.clocks:
      return ClockSignal(val, ClockType())
    return _FromCirctValue(val)

  def _set_output(self, idx, signal):
    assert signal is not None
    pname, ptype = self._builder.outputs[idx]
    if isinstance(signal, Signal):
      if ptype != signal.type:
        raise PortError(
            f"Input port {pname} expected type {ptype}, not {signal.type}")
    else:
      signal = ptype(signal)
    self._output_values[idx] = signal

  def _set_outputs(self, signal_dict: Dict[str, Signal]):
    """Set outputs from a dictionary of port names to signals."""
    for name, signal in signal_dict.items():
      if name not in self._output_port_lookup:
        raise PortError(f"Could not find output port '{name}'")
      idx = self._output_port_lookup[name]
      self._set_output(idx, signal)

  def _check_unconnected_outputs(self):
    unconnected_ports = []
    for idx, value in enumerate(self._output_values):
      if value is None:
        unconnected_ports.append(self._builder.outputs[idx][0])
    if len(unconnected_ports) > 0:
      raise support.UnconnectedSignalError(self._name, unconnected_ports)

  def _clear(self):
    """TL;DR: Downgrade a shotgun to a handgun.

    Instances are not _supposed_ to be held on beyond the end of generators...
    but at least one user will try. This method clears the contents of this
    class to prevent users from encountering a totally bizzare, unrelated error
    message when they make this mistake. If users reach into this class and hold
    on to private references... well, we did what we could to prevent their foot
    damage."""

    self._block_args = None
    self._output_values = None
    self._builder = None


class ModuleLikeBuilderBase(_PyProxy):
  """`ModuleLikeBuilder`s are responsible for preparing `Module` and other
  module-like subclasses for use. They are responsible for scanning the
  subclass' attribute, recognizing certain types (e.g. `InputPort`), and taking
  actions/mutating the subclass based on that information. They are also
  responsible for creating CIRCT IR -- creating the initial op, generating the
  bodies, instantiating modules, etc.

  This is the base class for common functionality which all 'ModuleLike` classes
  are likely to need. Each `ModuleLike` type will need to subclass this base.
  For instance, plain 'ol `Module`s have a corresponding subclass called
  `ModuleBuilder`. The correspondence is given by the `BuilderType` class
  variable in `Module`."""

  def __init__(self, cls, cls_dct, loc):
    from .types import Type
    self.modcls = cls
    self.cls_dct = cls_dct
    self.loc = loc

    self.outputs: Optional[List[Tuple[str, Type]]] = None
    self.inputs: Optional[List[Tuple[str, Type]]] = None
    self.clocks: Optional[Set[int]] = None
    self.resets: Optional[Set[int]] = None
    self.generators = None
    self.generator_port_proxy = None
    self.parameters = None
    self.attributes: Dict = {
        "output_file":
            hw.OutputFileAttr.get_from_filename(
                ir.StringAttr.get(f"{cls.__name__}.sv"), False, True)
    }

  def go(self):
    """Execute the analysis and mutation to make a `ModuleLike` class operate
    as such."""

    self.scan_cls()
    self.generator_port_proxy = self.create_port_proxy()
    self.add_external_port_accessors()

  def scan_cls(self):
    """Scan the class for input/output ports and generators. (Most `ModuleLike`
    will use these.) Store the results for later use."""

    input_ports = []
    output_ports = []
    clock_ports = set()
    reset_ports = set()
    generators = {}
    for attr_name, attr in self.cls_dct.items():
      if attr_name.startswith("_"):
        continue

      if attr_name == "Attributes":
        self.attributes = {
            mod_attr[0]: _obj_to_attribute(mod_attr[1])
            for mod_attr in attr
            if isinstance(mod_attr, tuple)
        }
        self.attributes.update({
            mod_attr: ir.UnitAttr.get()
            for mod_attr in attr
            if not isinstance(mod_attr, tuple)
        })
        continue

      if isinstance(attr, Clock):
        clock_ports.add(len(input_ports))
        input_ports.append((attr_name, attr.type))
      elif isinstance(attr, Reset):
        reset_ports.add(len(input_ports))
        input_ports.append((attr_name, attr.type))
      elif isinstance(attr, Input):
        input_ports.append((attr_name, attr.type))
      elif isinstance(attr, Output):
        output_ports.append((attr_name, attr.type))
      elif isinstance(attr, Generator):
        generators[attr_name] = attr

    self.outputs = output_ports
    self.inputs = input_ports
    self.clocks = clock_ports
    self.resets = reset_ports
    self.generators = generators

  def create_port_proxy(self):
    """Create a proxy class for generators to use in order to access module
    ports. Instances of this will (usually) be used in place of the `self`
    argument in generator calls.

    Replaces the dynamic lookup scheme previously utilized. Should be faster and
    (more importantly) reduces the amount of bookkeeping necessary."""

    proxy_attrs = {}
    for idx, (name, port_type) in enumerate(self.inputs):
      proxy_attrs[name] = property(lambda self, idx=idx: self._get_input(idx))

    output_port_lookup: Dict[str, int] = {}
    for idx, (name, port_type) in enumerate(self.outputs):

      def fset(self, val, idx=idx):
        self._set_output(idx, val)

      proxy_attrs[name] = property(fget=None, fset=fset)
      output_port_lookup[name] = idx
    proxy_attrs["_output_port_lookup"] = output_port_lookup
    proxy_attrs["_name"] = self.modcls.__name__

    return type(self.modcls.__name__ + "Ports", (PortProxyBase,), proxy_attrs)

  def add_external_port_accessors(self):
    """For each port, replace it with a property to provide access to the
    instances output in OTHER generators which are instantiating this module."""

    for idx, (name, port_type) in enumerate(self.inputs):

      def fget(self):
        raise PortError("Cannot access signal via instance input")

      setattr(self.modcls, name, property(fget=fget))

    named_outputs = {}
    for idx, (name, port_type) in enumerate(self.outputs):

      def fget(self, idx=idx):
        return _FromCirctValue(self.inst.operation.results[idx])

      named_outputs[name] = fget
      setattr(self.modcls, name, property(fget=fget))
    setattr(self.modcls,
            "outputs",
            lambda self, outputs=named_outputs:
            {n: g(self) for n, g in outputs.items()})

  @property
  def name(self):
    if hasattr(self.modcls, "module_name"):
      return self.modcls.module_name
    elif self.parameters is not None and len(self.generators) > 0:
      return _create_module_name(self.modcls.__name__, self.parameters)
    else:
      return self.modcls.__name__

  def print(self, out):
    print(
        f"<pycde.Module: {self.name} inputs: {self.inputs} "
        f"outputs: {self.outputs}>",
        file=out)

  class GeneratorCtxt:
    """Provides an context which most genertors need."""

    def __init__(self, builder: ModuleLikeBuilderBase, ports: PortProxyBase, ip,
                 loc: ir.Location) -> None:
      self.bc = _BlockContext()
      self.bb = BackedgeBuilder()
      self.ip = ir.InsertionPoint(ip)
      self.loc = loc
      self.clk = None
      self.ports = ports
      if builder.clocks is not None and len(builder.clocks) == 1:
        # Enter clock block implicitly if only one clock given.
        clk_port = list(builder.clocks)[0]
        self.clk = ClockSignal(ports._block_args[clk_port], ClockType())

    def __enter__(self):
      self.bc.__enter__()
      self.bb.__enter__()
      self.ip.__enter__()
      self.loc.__enter__()
      if self.clk is not None:
        self.clk.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
      if self.clk is not None:
        self.clk.__exit__(exc_type, exc_value, traceback)
      self.loc.__exit__(exc_type, exc_value, traceback)
      self.ip.__exit__(exc_type, exc_value, traceback)
      self.bb.__exit__(exc_type, exc_value, traceback)
      self.bc.__exit__(exc_type, exc_value, traceback)
      self.ports._clear()


class ModuleLikeType(type):
  """ModuleLikeType is a metaclass for Module and other things which look like
  modules (e.g. ServiceImplementations). A metaclass is nice since it gets run
  on each class (including subclasses), so has the ability to modify it. This is
  in contrast to a decorator which gets run once. It also has the advantage of
  being able to pretty easily extend `Module` or create an entirely new
  `ModuleLike` hierarchy.

  This metaclass essentially just kicks the brunt of the work over to a
  specified `ModuleLikeBuilder`, which can -- unlike metaclasses -- have state.
  Presumably, the usual thing is to store all of this state in the class itself,
  but we need this state to be private. Given that this isn't possible in
  Python, a single '_' variable is as small a surface area as we can get."""

  def __init__(cls, name, bases, dct: Dict):
    super(ModuleLikeType, cls).__init__(name, bases, dct)
    cls._builder = cls.BuilderType(cls, dct, get_user_loc())
    cls._builder.go()


class ModuleBuilder(ModuleLikeBuilderBase):
  """Defines how a `Module` gets built. Extend the base class and customize."""

  @property
  def circt_mod(self):
    """Get the raw CIRCT operation for the module definition. DO NOT store the
    returned value!!! It needs to get reaped after the current action (e.g.
    instantiation, generation). Memory safety when interacting with native code
    can be painful."""

    from .system import System
    sys: System = System.current()
    ret = sys._op_cache.get_circt_mod(self)
    if ret is None:
      return sys._create_circt_mod(self)
    return ret

  def add_metadata(self, sys, symbol: str, meta: Optional[Metadata]):
    """Add the metadata to the IR so it potentially gets included in the
    manifest. (It'll only be included if one of the instances has an appid.) If
    user did not specify the metadata (or components thereof), attempt to fill
    them in automatically:
      - Name defaults to the module name.
      - Summary defaults to the module docstring.
      - If GitPython is installed, the commit hash and repo are automatically
        generated if neither are specified.
    """

    from .dialects.esi import esi

    if meta is None:
      meta = Metadata()
    elif not isinstance(meta, Metadata):
      raise TypeError("Module metadata must be of type Metadata")

    if meta.name is None:
      meta.name = self.modcls.__name__

    try:
      # Attempt to automatically generate repo and commit hash using GitPython.
      if meta.repo is None and meta.commit_hash is None:
        import git
        import inspect
        modclsmodule = inspect.getmodule(self.modcls)
        if modclsmodule is not None:
          r = git.Repo(os.path.dirname(modclsmodule.__file__),
                       search_parent_directories=True)
          if r is not None:
            meta.repo = r.remotes.origin.url
            meta.commit_hash = r.head.object.hexsha
    except Exception:
      pass

    if meta.summary is None and self.modcls.__doc__ is not None:
      meta.summary = self.modcls.__doc__

    with ir.InsertionPoint(sys.mod.body):
      meta_op = esi.SymbolMetadataOp(
          symbolRef=ir.FlatSymbolRefAttr.get(symbol),
          name=ir.StringAttr.get(meta.name),
          repo=ir.StringAttr.get(meta.repo) if meta.repo is not None else None,
          commitHash=ir.StringAttr.get(meta.commit_hash)
          if meta.commit_hash is not None else None,
          version=ir.StringAttr.get(meta.version)
          if meta.version is not None else None,
          summary=ir.StringAttr.get(meta.summary)
          if meta.summary is not None else None)
      if meta.misc is not None:
        for k, v in meta.misc.items():
          meta_op.attributes[k] = _obj_to_attribute(v)

  def create_op(self, sys, symbol):
    """Callback for creating a module op."""

    if hasattr(self.modcls, "metadata"):
      meta = self.modcls.metadata
      self.add_metadata(sys, symbol, meta)
    else:
      self.add_metadata(sys, symbol, None)

    if len(self.generators) > 0:
      if hasattr(self, "parameters") and self.parameters is not None:
        self.attributes["pycde.parameters"] = self.parameters
      # If this Module has a generator, it's a real module.
      return hw.HWModuleOp(
          symbol,
          [(n, t._type) for (n, t) in self.inputs],
          [(n, t._type) for (n, t) in self.outputs],
          attributes=self.attributes,
          loc=self.loc,
          ip=sys._get_ip(),
      )

    # Modules without generators are implicitly considered to be external.
    if self.parameters is None:
      paramdecl_list = []
    else:
      paramdecl_list = [
          hw.ParamDeclAttr.get_nodefault(i.name, i.attr.type)
          for i in self.parameters
      ]
    self.attributes["verilogName"] = ir.StringAttr.get(self.name)
    self.attributes: Dict = {
        "output_file":
            hw.OutputFileAttr.get_from_filename(
                ir.StringAttr.get("external_modules.sv"), False, True)
    }
    return hw.HWModuleExternOp(
        symbol,
        input_ports=[(n, t._type) for (n, t) in self.inputs],
        output_ports=[(n, t._type) for (n, t) in self.outputs],
        parameters=paramdecl_list,
        attributes=self.attributes,
        loc=self.loc,
        ip=sys._get_ip(),
    )

  def instantiate(self, module_inst, inputs, instance_name: str):
    """"Instantiate this Module. Check that the input types match expectations."""

    port_input_lookup = {name: ptype for name, ptype in self.inputs}
    circt_inputs = {}
    for name, signal in inputs.items():
      if name not in port_input_lookup:
        raise PortError(f"Input port {name} not found in module")
      ptype = port_input_lookup[name]
      if isinstance(signal, Signal):
        # If the input is a signal, the types must match.
        if signal.type._type != ptype._type:
          raise ValueError(
              f"Wrong type on input signal '{name}'. Got '{signal.type}',"
              f" expected '{ptype}'")
        circt_inputs[name] = signal.value
      elif signal is None:
        if len(self.generators) > 0:
          raise PortError(
              f"Port {name} cannot be None (disconnected ports only allowed "
              "on extern mods.")
        circt_inputs[name] = create_const_zero(ptype)
      else:
        # If it's not a signal, assume the user wants to specify a constant and
        # try to convert it to a hardware constant.
        circt_inputs[name] = ptype(signal).value

    missing = list(
        filter(lambda name: name not in circt_inputs, port_input_lookup.keys()))
    if len(missing) > 0:
      raise ValueError(f"Missing input signals for ports: {', '.join(missing)}")

    circt_mod = self.circt_mod
    parameters = {}
    # If this is a parameterized external module, the parameters must be
    # supplied.
    if len(self.generators) == 0 and self.parameters is not None:
      parameters = self.parameters
    inst = hw.InstanceBuilder(circt_mod,
                              instance_name,
                              circt_inputs,
                              parameters=parameters,
                              sym_name=instance_name,
                              loc=get_user_loc())
    inst.operation.verify()
    return inst

  def generate(self):
    """Fill in (generate) this module. Only supports a single generator
    currently."""
    assert len(self.generators) == 1
    g: Generator = list(self.generators.values())[0]

    entry_block = self.circt_mod.add_entry_block()
    ports = self.generator_port_proxy(entry_block.arguments, self)
    with self.GeneratorCtxt(self, ports, entry_block, g.loc):
      outputs = g.gen_func(ports)
      if outputs is not None:
        raise ValueError("Generators must not return a value")

      ports._check_unconnected_outputs()
      hw.OutputOp([o.value for o in ports._output_values])


class Module(_PyProxy, metaclass=ModuleLikeType):
  """Subclass this class to define a regular PyCDE or external module. To define
  a module in PyCDE, supply a `@generator` method. To create an external module,
  don't. In either case, a list of ports is required.

  A few important notes:
  - If your subclass overrides the constructor, it MUST call the parent
  constructor AND pass through all of the input port signals to said parent
  constructor. Using kwargs (e.g. **inputs) is the easiest way to fulfill this
  requirement.
  - If you have a @generator, you MUST NOT hold on to, store, or otherwise leak
  the first argument (i.e. self) beyond the function return. It is a special
  instance constructed exclusively for the generator.
  """

  BuilderType = ModuleBuilder
  _builder: ModuleBuilder

  def __init__(self, instance_name: str = None, appid: AppID = None, **inputs):
    """Create an instance of this module. Instance namd and appid are optional.
    All inputs must be specified. If a signal has not been produced yet, use the
    `Wire` construct and assign the signal to that wire later on."""
    from .system import System

    kwargs = dict()

    # Figure out what the 'instantiate' method expects and then provide it.
    self.sig = inspect.signature(self._builder.instantiate)
    for (_, param) in self.sig.parameters.items():
      if param.name == "instance_name":
        # Create a valid instance name.
        if instance_name is None:
          if hasattr(self, "instance_name"):
            instance_name = self.instance_name
          else:
            instance_name = self.__class__.__name__
        kwargs["instance_name"] = _BlockContext.current().uniquify_symbol(
            instance_name)
      elif param.name == "appid":
        # Pass through the appid if it was provided.
        kwargs["appid"] = appid

    self.inst = self._builder.instantiate(self, inputs, **kwargs)
    if appid is not None:
      self.inst.operation.attributes[AppID.AttributeName] = appid._appid

    System.current()._op_cache.register_pyproxy(self)

  def clear_op_refs(self):
    self.inst = None

  @classmethod
  def print(cls, out=sys.stdout):
    cls._builder.print(out)

  @classmethod
  def inputs(cls) -> List[Tuple[str, Type]]:
    """Get a dictionary of input port names to signals."""
    if cls._builder.inputs is None:
      return []
    return cls._builder.inputs


class modparams:
  """Decorate a function to indicate that it is returning a Module which is
  parameterized by this function. Arguments to this class MUST be convertible to
  a recognizable constant. Ideally, they would be simple since (by default) they
  will be turned into strings and appended to the module name in the resulting
  RTL. Arguments with underscore prefixes are ignored and thus exempt from the
  previous requirement."""

  func = None

  # When the decorator is attached, this runs.
  def __init__(self, func: builtins.function):

    # If it's a module parameterization function, inspect the arguments to
    # ensure sanity.
    self.func = func
    self.sig = inspect.signature(self.func)
    for (_, param) in self.sig.parameters.items():
      if param.kind == param.VAR_KEYWORD:
        raise TypeError("Module parameter definitions cannot have **kwargs")
      if param.kind == param.VAR_POSITIONAL:
        raise TypeError("Module parameter definitions cannot have *args")

  # This function gets executed in two situations:
  #   - In the case of a module function parameterizer, it is called when the
  #   user wants to apply specific parameters to the module. In this case, we
  #   should call the function, wrap the returned module class, and return it.
  #   The result is cached in _MODULE_CACHE.
  #   - A simple (non-parameterized) module has been wrapped and the user wants
  #   to construct one. Just forward to the module class' constructor.
  def __call__(self, *args, **kwargs):
    assert self.func is not None
    param_values = self.sig.bind(*args, **kwargs)
    param_values.apply_defaults()

    # Function arguments which start with '_' don't become parameters.
    params = {
        n: v for n, v in param_values.arguments.items() if not n.startswith("_")
    }

    # Check cache
    cache_key = _get_module_cache_key(self.func, params)
    if cache_key in _MODULE_CACHE:
      return _MODULE_CACHE[cache_key]

    cls = self.func(*args, **kwargs)
    if not issubclass(cls, Module):
      raise ValueError("Parameterization function must return Module class")

    cls._builder.parameters = cache_key[1]
    _MODULE_CACHE[cache_key] = cls
    return cls


@dataclass
class Metadata:
  """Metadata for a module. This is used to provide information about a module
  in the ESI manifest. Set the classvar 'metadata' to an instance of this class
  to provide metadata for a module."""

  name: Optional[str] = None
  repo: Optional[str] = None
  commit_hash: Optional[str] = None
  version: Optional[str] = None
  summary: Optional[str] = None
  misc: Optional[Dict[str, Any]] = None


class ImportedModSpec(ModuleBuilder):
  """Specialization to support imported CIRCT modules."""

  # Creation callback that just moves the already build module into the System's
  # ModuleOp and returns it.
  def create_op(self, sys, symbol: str):
    hw_module = self.modcls.hw_module

    # TODO: deal with symbolrefs to this (potentially renamed) module symbol.
    sys.mod.body.append(hw_module)

    # Need to clear out the reference to ourselves so that we can release the
    # raw reference to `hw_module`. It's safe to do so since unlike true PyCDE
    # modules, this can only be run once during the import_mlir.
    self.modcls.hw_module = None
    return hw_module

  def instantiate(self, module_inst, inputs, instance_name: str):
    inst = self.circt_mod.instantiate(
        instance_name,
        **{
            n: i.value if isinstance(i, Signal) else i
            for (n, i) in inputs.items()
        },
        parameters={} if self.parameters is None else self.parameters,
        loc=get_user_loc())
    inst.operation.verify()
    return inst.operation


def import_hw_module(hw_module: hw.HWModuleOp):
  """Import a CIRCT module into PyCDE. Returns a standard Module subclass which
  operates just like an external PyCDE module.

  For now, the imported module name MUST NOT conflict with any other modules.
  
  THIS IS BROKEN: https://github.com/llvm/circt/issues/6130"""
  # TODO: fix me

  # Get the module name to use in the generated class and as the external name.
  name = ir.StringAttr(hw_module.name).value

  # Collect input and output ports as named Inputs and Outputs.
  modattrs = {}
  for input_name, block_arg in hw_module.inputs().items():
    modattrs[input_name] = Input(block_arg.type, input_name)
  for output_name, output_type in hw_module.outputs().items():
    modattrs[output_name] = Output(output_type, output_name)
  modattrs["BuilderType"] = ImportedModSpec
  modattrs["hw_module"] = hw_module

  # Use the name and ports to construct a class object like what externmodule
  # would wrap.
  cls = type(name, (Module,), modattrs)

  return cls
