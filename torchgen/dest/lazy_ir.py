from abc import ABC
from typing import List, Union, Tuple
from dataclasses import dataclass
from torchgen.context import method_with_native_function
from torchgen.model import (
    Argument,
    BackendIndex,
    NativeFunction,
    NativeFunctionsGroup,
    Tag,
)
from torchgen.api.types import (
    BaseCType,
    OptionalCType,
    DispatcherSignature,
    VectorCType,
    kernel_signature,
    Binding,
)
import torchgen.api.dispatcher as dispatcher
from tools.codegen.api.translate import translate
from torchgen.api.lazy import (
    LazyIrSchema,
    LazyArgument,
    isValueType,
    tensorListValueT,
)
from torchgen.dest.lazy_ts_lowering import ts_lowering_body


def node_ctor_arg_rvalue_string(arg: LazyArgument) -> str:
    """
    Given a LazyArgument,
    generate a c++ string for materializing an rvalue of that arg for passing into
    a lazy Node constructor.
    """

    if isValueType(arg.lazy_type):
        if isinstance(arg.lazy_type, BaseCType):
            if arg.is_wrapped_scalar:
                return f"torch::lazy::LazyGraphExecutor::Get()->GetIrValueForScalarFromCodegen({arg.name})"
            elif arg.lazy_type.type is tensorListValueT:
                return f"lazy_{arg.name}_tensorlist"
            return f"lazy_{arg.name}->GetIrValue()"
        elif isinstance(arg.lazy_type, OptionalCType):
            if arg.is_wrapped_scalar:
                return (
                    f"{arg.name} ? "
                    f"c10::make_optional(torch::lazy::LazyGraphExecutor::Get()->GetIrValueForScalarFromCodegen(*{arg.name})) : "
                    "c10::nullopt"
                )
            return (
                f"lazy_{arg.name} ? "
                f"c10::make_optional(lazy_{arg.name}->GetIrValue()) : "
                "c10::nullopt"
            )
        else:
            raise AssertionError(
                f"TODO not sure if there are other valid types to handle here ({arg.lazy_type})"
            )
    else:
        if isinstance(arg.lazy_type, VectorCType) and isinstance(
            arg.lazy_type.elem, BaseCType
        ):
            return f"std::vector<{arg.lazy_type.elem.type}>({arg.name}.begin(), {arg.name}.end())"
        elif (
            isinstance(arg.lazy_type, OptionalCType)
            and isinstance(arg.lazy_type.elem, VectorCType)
            and isinstance(arg.lazy_type.elem.elem, BaseCType)
        ):
            return f"torch::lazy::ToOptionalVector<{arg.lazy_type.elem.elem.type}>({arg.name})"
        else:
            return f"{arg.name}"


def node_ctor_inputs(schema: LazyIrSchema) -> str:
    """
    Produce a formatted string with the arguments as passed into the constructor of a node class.
    """
    node_ctor_values = [
        node_ctor_arg_rvalue_string(arg) for arg in schema.filtered_args()
    ]
    return ",\n                              ".join(node_ctor_values)


def gen_fallback_code(schema: LazyIrSchema, overload_name: str) -> str:
    """
    Generate code that falls back to eager conditioned on a predicate
    """
    fallback_args = ",\n                ".join(
        [str(arg.name) for arg in schema.filtered_args(generator=True)]
    )
    if len(overload_name):
        aten_op_str = f"ATEN_OP2({schema.aten_name}, {overload_name})"
    else:
        aten_op_str = f"ATEN_OP({schema.aten_name})"
    or_has_generator = ""
    if schema.generator_arg:
        # generators are always optional and there is never more than one, at least currently
        or_has_generator = f" || ({schema.generator_arg.name}.has_value() && {schema.generator_arg.name}->defined())"
    return f"""
        if (force_eager_fallback({aten_symbol(schema)}){or_has_generator}) {{
            return at::native::call_fallback_fn<&ltc_eager_fallback, {aten_op_str}>::call(
                {fallback_args}
            );
        }}
"""


def aten_symbol(schema: LazyIrSchema) -> str:
    missing_interned_strings = {
        "sigmoid_backward",
    }
    if schema.aten_name in missing_interned_strings:
        return f'c10::Symbol::fromQualString("aten::{schema.aten_name}")'
    return f"at::aten::{schema.aten_name}"


@dataclass(frozen=True)
class GenLazyIR(ABC):
    backend_index: BackendIndex
    node_base: str

    @method_with_native_function
    def __call__(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        return self.gen(f)

    # there is no lowering functionality generated unless this IR base class is subclassed and
    # implemented as a backend-specific node
    def lowering_function(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> str:
        return ""

    def node_base_ctor_call(self, schema: LazyIrSchema) -> str:
        # backends can customize the way the node base class constructor is called,
        # as long as all of its arguments can be generated from information available from the schema
        return f"{self.node_base}(torch::lazy::OpKind({aten_symbol(schema)})"

    def gen(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> List[str]:
        # for now, we just want one IR class decl and soon after also the method defs
        # and we use the functional version not out/inplace.
        func = f.functional.func if isinstance(f, NativeFunctionsGroup) else f.func
        schema = LazyIrSchema(func)
        all_args = schema.filtered_args()
        value_args = schema.filtered_args(values=True, scalars=False)
        scalar_args = schema.filtered_args(values=False, scalars=True)

        node_ctor_args = ", ".join(
            [f"const {i.lazy_type.cpp_type()}& {i.name}" for i in all_args]
        )
        scalar_initializers = ",\n        ".join(
            [f"{a.name}({a.name})" for a in scalar_args]
        )
        comma_if_scalar_initializers = ",\n" if len(scalar_initializers) else ""
        scalar_decls = "\n  ".join(
            [
                f"std::string {a.name};"
                if a.lazy_type.cpp_type() == "c10::string_view"
                else f"{a.lazy_type.cpp_type()} {a.name};"
                for a in scalar_args
            ]
        )
        scalar_hashes = ", ".join([f"{a.name}" for a in scalar_args])
        base_ctor_value_args_list = []
        optional_values = []
        for arg in value_args:
            if isinstance(arg.lazy_type, BaseCType) or isinstance(
                arg.lazy_type, VectorCType
            ):
                base_ctor_value_args_list.append(f"{arg.name}")
            elif isinstance(arg.lazy_type, OptionalCType):
                base_ctor_value_args_list.append(f"{arg.name}.value_or(kNullValue)")
                optional_values.append(arg.name)
            else:
                raise AssertionError(
                    f"TODO not sure if there are other valid types to handle here ({arg.lazy_type})"
                )
        base_ctor_value_args = ", ".join(base_ctor_value_args_list)
        has_optional_decls = "\n  ".join(
            [f"bool has_{value}: 1;" for value in optional_values]
        )
        has_optional_defs = "\n    ".join(
            [f"has_{value} = !!{value};" for value in optional_values]
        )
        members_to_string = []
        for arg in scalar_args:
            if isinstance(arg.lazy_type, OptionalCType):
                members_to_string.append(
                    f"""if ({arg.name}.has_value()) {{
    ss << ", {arg.name}=" << {arg.name}.value();
}} else {{
    ss << ", {arg.name}=null";
}}"""
                )
            else:
                members_to_string.append(f'ss << ", {arg.name}=" << {arg.name};')
        members_to_string_str = "\n    ".join(members_to_string)

        return [
            f"""\
class {schema.node_name} : public {self.node_base} {{
 public:
  {schema.node_name}({node_ctor_args}, std::vector<Shape>&& shapes)
      : {self.node_base_ctor_call(schema)},
              {{{base_ctor_value_args}}}, std::move(shapes),
              /* num_outputs */ {len(func.returns)},
              torch::lazy::MHash({scalar_hashes})){comma_if_scalar_initializers}
        {scalar_initializers}

  {{
    {has_optional_defs}
  }}

  std::string ToString() const override {{
    std::stringstream ss;
    ss << {self.node_base}::ToString();
    {members_to_string_str}
    return ss.str();
  }}

  {self.lowering_function(f)}

  {scalar_decls}
  {has_optional_decls}

}};

""",
        ]


@dataclass(frozen=True)
class GenTSLazyIR(GenLazyIR):
    def lowering_function(self, f: Union[NativeFunctionsGroup, NativeFunction]) -> str:
        return f"""torch::lazy::TSOpVector Lower(std::shared_ptr<torch::jit::GraphFunction> function,
    torch::lazy::TSLoweringContext* loctx) const override {{
    {ts_lowering_body(f)}
  }}"""


<<<<<<< HEAD:torchgen/dest/lazy_ir.py
=======
def lazy_tensor_decls(value_args: List[LazyArgument], tensor_class: str) -> str:
    lazy_tensor_decls: List[str] = []
    for arg in value_args:
        if arg.is_wrapped_scalar:
            # no lazy tensor wrapper for scalars that are promoted to IR values
            continue
        elif isinstance(arg.lazy_type, BaseCType):
            if arg.lazy_type.type is tensorListValueT:
                lazy_tensor_decls.append(
                    f"auto lazy_{arg.name}_tensorlist = torch::lazy::GetTensorList({arg.name});"
                )
            else:
                lazy_tensor_decls.append(
                    f"{tensor_class}Ptr lazy_{arg.name} = "
                    f"torch::lazy::GetLtcTensorOrCreateForWrappedNumber({arg.name}, *common_device);"
                )
        elif isinstance(arg.lazy_type, OptionalCType):
            # TODO(alanwaketan): Maybe we want to apply GetLtcTensorOrCreateForWrappedNumber here, but hold it
            # until we encounter a real world example.
            lazy_tensor_decls.append(
                f"    {tensor_class}Ptr lazy_{arg.name} = torch::lazy::TryGetLtcTensor({arg.name}.value_or(at::Tensor()));"
            )
        else:
            raise AssertionError(
                f"TODO not sure if there are other valid types to handle here ({arg.lazy_type})"
            )
    return ("\n        ").join(lazy_tensor_decls)


# converts  all tensor-like arguments to meta tensors. Returns:
# (1) a string containing all of the logic that does the conversions.
# (2) a context, to be used by translate(), with all of the relevant bindings.
def convert_to_meta_tensors(sig: DispatcherSignature) -> Tuple[str, List[Binding]]:
    context: List[Binding] = []
    unwrapped_tensor_args: List[str] = []
    for arg in sig.arguments():
        if isinstance(arg.argument, Argument) and arg.argument.type.is_tensor_like():
            unwrapped_name = f"{arg.name}_meta"
            unwrapped_tensor_args.append(
                f"auto {unwrapped_name} = at::native::to_meta({arg.name});"
            )
            context.append(arg.with_name(unwrapped_name))
        else:
            context.append(arg)
    unwrap_tensor_args_str = "\n        ".join(unwrapped_tensor_args)
    return unwrap_tensor_args_str, context


>>>>>>> d7fb69c729 ([prototype] integrate functionalization <> LTC torchscript backend):tools/codegen/dest/lazy_ir.py
@dataclass(frozen=True)
class GenLazyNativeFuncDefinition:
    class_method_name: str
    backend_index: BackendIndex
    tensor_class: str
    gen_forced_fallback_code: bool
    backend_namespace: str
    get_tensorlist: str
    get_tensor_or_wrap_number: str
    try_get_tensor: str
    metrics_counter: str
    create_tensor: str
    create_from_first_tensor: bool

    def lazy_tensor_decls(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        value_args = schema.filtered_args(values=True, scalars=False)
        # Generates lazy_{name} variables for LazyTensors wrapping input tensors
        lazy_tensor_decls: List[str] = []
        for arg in value_args:
            if arg.is_wrapped_scalar:
                # no lazy tensor wrapper for scalars that are promoted to IR values
                continue
            elif isinstance(arg.lazy_type, BaseCType):
                if arg.lazy_type.type is tensorListValueT:
                    lazy_tensor_decls.append(
                        f"auto lazy_{arg.name}_tensorlist = "
                        f"{self.backend_namespace}::{self.get_tensorlist}({arg.name});"
                    )
                else:
                    lazy_tensor_decls.append(
                        f"{self.tensor_class}Ptr lazy_{arg.name} = "
                        f"{self.backend_namespace}::{self.get_tensor_or_wrap_number}({arg.name}, *common_device);"
                    )
            elif isinstance(arg.lazy_type, OptionalCType):
                # TODO(alanwaketan): Maybe we want to apply GetLtcTensorOrCreateForWrappedNumber here, but hold it
                # until we encounter a real world example.
                lazy_tensor_decls.append(
                    f"    {self.tensor_class}Ptr lazy_{arg.name} = "
                    f"{self.backend_namespace}::{self.try_get_tensor}({arg.name}.value_or(at::Tensor()));"
                )
            else:
                raise AssertionError(
                    f"TODO not sure if there are other valid types to handle here ({arg.lazy_type})"
                )
        return ("\n        ").join(lazy_tensor_decls)

    def force_eager_fallback(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        if self.gen_forced_fallback_code:
            return gen_fallback_code(schema, overload_name=func.func.name.overload_name)
        return ""

    def metrics(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        return f'{self.metrics_counter};'

    def get_device(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        value_args = schema.filtered_args(values=True, scalars=False)
        value_types_names = [f"{a.name}" for a in value_args if not a.is_wrapped_scalar]
        assert len(value_types_names) > 0, "Code below assumes there is at least one tensor arg"
        return f"""auto common_device = torch::lazy::GetBackendDevice({', '.join(value_types_names)});
        TORCH_INTERNAL_ASSERT(common_device);
        """

    def shape_inference(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        metadata = self.backend_index.get_kernel(func)
        assert metadata is not None
        all_args = schema.filtered_args()
        returns_length = len(schema.returns)
<<<<<<< HEAD:torchgen/dest/lazy_ir.py
        # call the meta kernel if it exists, to compute output shape/dtype for our IR
        if func.structured or func.structured_delegate is not None:
=======

        # Note [Generated LTC Shape Functions]
        # LTC uses meta tensors from core to do shape inference when possible, and otherwise
        # we generate a shape function declaration that needs to be manually implemented.
        # How do we detect which ops are eligible to use meta tensors?
        # In general we should be able to use meta tensors not just on structured operators,
        # but also on composite operators that are implemented in terms of structured kernels.
        # We don't currently have a way of knowing at codegen time which ops are implemented that way.
        # This is the case for all view and view_copy operators however, so we're going to
        # use them specifically for al of the view_copy ops (instead of manually writing shape rules for all of them).
        is_view_copy_op = func.tag == Tag.view_copy
        is_structured = func.structured or func.structured_delegate is not None
        if is_structured or is_view_copy_op:
>>>>>>> d7fb69c729 ([prototype] integrate functionalization <> LTC torchscript backend):tools/codegen/dest/lazy_ir.py
            meta_out = """std::vector<Shape> shapes{Shape(out_meta.scalar_type(), out_meta.sizes().vec())};"""
            if returns_length > 1:

                def this_shape(i: int) -> str:
                    return f"Shape(std::get<{i}>(out_meta).scalar_type(), std::get<{i}>(out_meta).sizes().vec())"

                shapes_str = ",".join([this_shape(i) for i in range(returns_length)])
                meta_out = "std::vector<Shape> shapes{" + shapes_str + "};"

<<<<<<< HEAD:torchgen/dest/lazy_ir.py
            shape_str = f"""auto out_meta = at::meta::{schema.aten_name}({', '.join(str(a.name) for a in all_args)});
=======
            # Convert tensor args to the meta device and call it.
            # (We can't pass in the input tensors directly, because they are "functional wrappers".
            # If any of the meta kernels call a tensor op and redispatch, we don't want to hit the functionalize kernels.)
            # Even at::meta:: functions might redispatch, e.g. if they call into view ops.
            dispatcher_sig = DispatcherSignature.from_schema(func.func)
            meta_conversion_str, meta_call_ctx = convert_to_meta_tensors(dispatcher_sig)
            meta_call_args = [
                e.expr
                for e in translate(
                    meta_call_ctx, dispatcher_sig.arguments(), method=False
                )
            ]
            dispatch_ns = "meta" if is_structured else "compositeexplicitautograd"
            # view_copy ops always have a CompositeExplicitAutograd kernel
            meta_str = f"""\
        {meta_conversion_str}
        auto out_meta = at::{dispatch_ns}::{schema.aten_name}({', '.join(meta_call_args)});
>>>>>>> d7fb69c729 ([prototype] integrate functionalization <> LTC torchscript backend):tools/codegen/dest/lazy_ir.py
        {meta_out}"""
        else:
            shape_sig = ComputeShapeSignature(metadata.kernel, func)
            shape_str = f"""
        auto shapes = {shape_sig.shape_call};"""

        shape_str += f"""
        TORCH_INTERNAL_ASSERT(shapes.size() == {returns_length});"""

        # Calculating which dimensions are symbolic
        func_schema_str = "aten::" + str(func.func)
        shape_str += f"""
        if(symbolicShapeEnabled()){{
            std::vector<jit::IValue> inputs = {{ {', '.join(str(a.name) for a in all_args)} }};
            char* schema_str = "{func_schema_str}";
            applySymbolicShapesOnLT(schema_str, inputs, shapes);
        }}
        """
        return shape_str

    def build_ir_node(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        node_ctor_input_str = node_ctor_inputs(schema)
        return f"""auto node = torch::lazy::MakeNode<{schema.node_name}>({node_ctor_input_str},
                                                                                      std::move(shapes));"""

    def create_lazy_tensor(self, first_tensor_name: str) -> str:
        # xla uses an instance method for tensor creation, for the time being
        if self.create_from_first_tensor:
            # TODO(whc) remove this if XLA switches to using static method for creation
            return f"{first_tensor_name}.{self.create_tensor}"
        return f"{self.backend_namespace}::{self.create_tensor}"

    def return_aten_tensor(self, func: NativeFunction, schema: LazyIrSchema) -> str:
        returns_length = len(schema.returns)
        value_args = schema.filtered_args(values=True, scalars=False)
        value_types_names = [f"{a.name}" for a in value_args if not a.is_wrapped_scalar]
        assert len(value_types_names) > 0, "Code below assumes there is at least one tensor arg"
        first_tensor_name = value_types_names[0]
        bridge_str = f"""auto result = torch::lazy::CreateAtenFromLtcTensor(
                {self.create_lazy_tensor(first_tensor_name)}(std::move(node), *common_device));"""

        if returns_length > 1:
            bridge_str = f"""std::vector<{self.tensor_class}Ptr> lazy_tensors;
        for (int i = 0; i < {returns_length}; i++) {{
            lazy_tensors.push_back({self.create_lazy_tensor(first_tensor_name)}(torch::lazy::Value(node, i), *common_device));
        }}
        auto result = torch::lazy::TupleAtenFromLtcTensors<{returns_length}>(lazy_tensors);"""

        if schema.name.name.inplace or func.func.is_out_fn():
            assert returns_length == 1, (
                "We assumed there was no such case where an op is an in-place variant "
                f"and has tuple outputs, but got tuple of len {returns_length}."
            )
            bridge_str = f"""lazy_{first_tensor_name}->SetInPlaceIrValue(node);
        auto& result = {first_tensor_name};"""

        bridge_str += """
        return result;"""
        return bridge_str

    @method_with_native_function
    def __call__(self, func: NativeFunction) -> List[str]:
        sig = kernel_signature(func, self.backend_index)
        metadata = self.backend_index.get_kernel(func)
        assert metadata is not None
        schema = LazyIrSchema(func.func)
        return [f"""\
    {sig.decl(name=f"{self.class_method_name}::{metadata.kernel}")} {{
        {self.force_eager_fallback(func, schema)}
        {self.metrics(func, schema)}
        {self.get_device(func, schema)}
        {self.lazy_tensor_decls(func, schema)}
        {self.shape_inference(func, schema)}
        {self.build_ir_node(func, schema)}
        {self.return_aten_tensor(func, schema)}
    }};\n
    """]


class ComputeShapeSignature:
    """
    Here we use the base name as the suffix of the signature to avoid generating for in-place variants.
    """

    def __init__(self, kernel_name: str, f: NativeFunction):
        self.__schema = LazyIrSchema(f.func)
        self.__dispatch_args = ", ".join(
            [a.decl() for a in dispatcher.arguments(f.func)]
        )
        self.__call_args = ", ".join(
            [f"{arg.name}" for arg in self.__schema.filtered_args(generator=True)]
        )
        self.__kernel_name = kernel_name

    def __decl_suffix(self) -> str:
        return f"{self.__kernel_name}({self.__dispatch_args})"

    def __call_suffix(self) -> str:
        return f"{self.__kernel_name}({self.__call_args})"

    @property
    def shape_decl(self) -> str:
        return f"TORCH_API std::vector<Shape> compute_shape_{self.__decl_suffix()}"

    @property
    def shape_call(self) -> str:
        return f"torch::lazy::compute_shape_{self.__call_suffix()}"


@dataclass(frozen=True)
class GenLazyShapeInferenceDefinition:
    backend_index: BackendIndex
    tensor_class: str

    @method_with_native_function
    def __call__(self, f: NativeFunction) -> List[str]:
        sig = kernel_signature(f, self.backend_index)
        metadata = self.backend_index.get_kernel(f)
        assert metadata is not None

        # See Note [Generated LTC Shape Functions]
        is_view_copy_op = (
            f.tag == Tag.view_copy and f.has_composite_explicit_autograd_kernel
        )
        is_structured = f.structured or f.structured_delegate is not None
        if is_structured or is_view_copy_op:
            return []
        else:
            shape_sig = ComputeShapeSignature(metadata.kernel, f)
            return ["\n".join([f"{shape_sig.shape_decl};"])]
