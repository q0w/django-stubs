import dataclasses
from abc import ABCMeta, abstractmethod
from typing import cast

from django.db.models.fields.related import ForeignKey
from mypy.newsemanal.semanal import NewSemanticAnalyzer
from mypy.nodes import ClassDef, MDEF, SymbolTableNode, TypeInfo, Var
from mypy.plugin import ClassDefContext
from mypy.types import Instance

from django.db.models.fields import Field
from mypy_django_plugin_newsemanal.django.context import DjangoContext
from mypy_django_plugin_newsemanal.lib import helpers
from mypy_django_plugin_newsemanal.transformers import fields
from mypy_django_plugin_newsemanal.transformers.fields import get_field_descriptor_types


@dataclasses.dataclass
class ModelClassInitializer(metaclass=ABCMeta):
    api: NewSemanticAnalyzer
    model_classdef: ClassDef
    django_context: DjangoContext
    ctx: ClassDefContext

    @classmethod
    def from_ctx(cls, ctx: ClassDefContext, django_context: DjangoContext):
        return cls(api=cast(NewSemanticAnalyzer, ctx.api),
                   model_classdef=ctx.cls,
                   django_context=django_context,
                   ctx=ctx)

    def lookup_typeinfo_or_incomplete_defn_error(self, fullname: str) -> TypeInfo:
        sym = self.api.lookup_fully_qualified_or_none(fullname)
        if sym is None or not isinstance(sym.node, TypeInfo):
            raise helpers.IncompleteDefnException(f'No {fullname!r} found')
        return sym.node

    def lookup_field_typeinfo_or_incomplete_defn_error(self, field: Field) -> TypeInfo:
        fullname = helpers.get_class_fullname(field.__class__)
        field_info = self.lookup_typeinfo_or_incomplete_defn_error(fullname)
        return field_info

    def add_new_node_to_model_class(self, name: str, typ: Instance) -> None:
        # type=: type of the variable itself
        var = Var(name=name, type=typ)
        # var.info: type of the object variable is bound to
        var.info = self.model_classdef.info
        var._fullname = self.model_classdef.info.fullname() + '.' + name
        var.is_initialized_in_class = True
        var.is_inferred = True
        self.model_classdef.info.names[name] = SymbolTableNode(MDEF, var, plugin_generated=True)

    @abstractmethod
    def run(self) -> None:
        raise NotImplementedError()


class InjectAnyAsBaseForNestedMeta(ModelClassInitializer):
    """
    Replaces
        class MyModel(models.Model):
            class Meta:
                pass
    with
        class MyModel(models.Model):
            class Meta(Any):
                pass
    to get around incompatible Meta inner classes for different models.
    """

    def run(self) -> None:
        meta_node = helpers.get_nested_meta_node_for_current_class(self.model_classdef.info)
        if meta_node is None:
            return None
        meta_node.fallback_to_any = True


class AddDefaultPrimaryKey(ModelClassInitializer):
    def run(self) -> None:
        model_cls = self.django_context.get_model_class_by_fullname(self.model_classdef.fullname)
        if model_cls is None:
            return

        auto_field = model_cls._meta.auto_field
        if auto_field and not self.model_classdef.info.has_readable_member(auto_field.attname):
            # autogenerated field
            auto_field_fullname = helpers.get_class_fullname(auto_field.__class__)
            auto_field_info = self.lookup_typeinfo_or_incomplete_defn_error(auto_field_fullname)

            set_type, get_type = fields.get_field_descriptor_types(auto_field_info, is_nullable=False)
            self.add_new_node_to_model_class(auto_field.attname, Instance(auto_field_info,
                                                                          [set_type, get_type]))


class AddRelatedModelsId(ModelClassInitializer):
    def run(self) -> None:
        model_cls = self.django_context.get_model_class_by_fullname(self.model_classdef.fullname)
        if model_cls is None:
            return

        for field in model_cls._meta.get_fields():
            if isinstance(field, ForeignKey):
                rel_primary_key_field = self.django_context.get_primary_key_field(field.related_model)
                field_info = self.lookup_field_typeinfo_or_incomplete_defn_error(rel_primary_key_field)
                is_nullable = self.django_context.fields_context.get_field_nullability(field, None)
                set_type, get_type = get_field_descriptor_types(field_info, is_nullable)
                self.add_new_node_to_model_class(field.attname,
                                                 Instance(field_info, [set_type, get_type]))


class AddManagers(ModelClassInitializer):
    def run(self):
        model_cls = self.django_context.get_model_class_by_fullname(self.model_classdef.fullname)
        if model_cls is None:
            return

        for manager_name, manager in model_cls._meta.managers_map.items():
            if manager_name not in self.model_classdef.info.names:
                manager_fullname = helpers.get_class_fullname(manager.__class__)
                manager_info = self.lookup_typeinfo_or_incomplete_defn_error(manager_fullname)

                manager = Instance(manager_info, [Instance(self.model_classdef.info, [])])
                self.add_new_node_to_model_class(manager_name, manager)

        # add _default_manager
        if '_default_manager' not in self.model_classdef.info.names:
            default_manager_fullname = helpers.get_class_fullname(model_cls._meta.default_manager.__class__)
            default_manager_info = self.lookup_typeinfo_or_incomplete_defn_error(default_manager_fullname)
            default_manager = Instance(default_manager_info, [Instance(self.model_classdef.info, [])])
            self.add_new_node_to_model_class('_default_manager', default_manager)


def process_model_class(ctx: ClassDefContext,
                        django_context: DjangoContext) -> None:
    initializers = [
        InjectAnyAsBaseForNestedMeta,
        AddDefaultPrimaryKey,
        AddRelatedModelsId,
        AddManagers,
    ]
    for initializer_cls in initializers:
        try:
            initializer_cls.from_ctx(ctx, django_context).run()
        except helpers.IncompleteDefnException:
            if not ctx.api.final_iteration:
                ctx.api.defer()
