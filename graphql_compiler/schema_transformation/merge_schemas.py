# Copyright 2019-present Kensho Technologies, LLC.
from collections import namedtuple
from copy import deepcopy

from graphql import build_ast_schema
from graphql.language import ast as ast_types
from graphql.language.printer import print_ast
from graphql.pyutils import FrozenList
import six

from ..ast_manipulation import get_ast_with_non_null_stripped
from ..compiler.helpers import INBOUND_EDGE_DIRECTION, OUTBOUND_EDGE_DIRECTION
from ..compiler.subclass import compute_subclass_sets
from .utils import (
    InvalidCrossSchemaEdgeError,
    SchemaNameConflictError,
    check_ast_schema_is_valid,
    check_schema_identifier_is_valid,
    get_query_type_name,
)


MergedSchemaDescriptor = namedtuple(
    "MergedSchemaDescriptor",
    (
        "schema_ast",  # Document, AST representing the merged schema
        "schema",  # GraphQLSchema, representing the same schema as schema_ast
        "type_name_to_schema_id",
        # Dict[str, str], mapping type name to the id of its schema, includes Interface, Object,
        # Union, and Enum types
    ),
)


CrossSchemaEdgeDescriptor = namedtuple(
    "CrossSchemaEdgeDescriptor",
    (
        "edge_name",  # str, name used for the corresponding in and out fields
        "outbound_field_reference",  # FieldReference namedtuple for the outbound field
        "inbound_field_reference",  # FieldReference namedtuple for the inbound field
        "out_edge_only",  # bool, whether or not the edge is bidirectional
    ),
)


FieldReference = namedtuple(
    "FieldReference",
    (
        "schema_id",  # str, identifier for the schema of the field
        "type_name",  # str, name of the object or interface that the field belongs to
        "field_name",  # str, name of the field, used in the stich directive
    ),
)


def merge_schemas(schema_id_to_ast, cross_schema_edges, type_equivalence_hints=None):
    """Merge all input schemas and add all cross-schema edges.

    The merged schema will contain all object, interface, union, enum, scalar, and directive
    definitions from input schemas. The fields of its query type will be the union of the
    fields of the query types of each input schema.

    Cross schema edges will be incorporated by adding vertex fields with a @stitch directive
    to appropriate vertex types. New fields will be named out_ or in_ concatenated
    with the edge name. New vertex fields will be added to the specified outbound and inbound
    vertices and to all of their subclass vertices.

    Args:
        schema_id_to_ast: OrderedDict[str, Document], where keys are names/identifiers of
                          schemas, and values are ASTs describing schemas. The ASTs will not
                          be modified by this function
        cross_schema_edges: List[CrossSchemaEdgeDescriptor], containing all edges connecting
                            fields in multiple schemas to be added to the merged schema
        type_equivalence_hints: Dict[GraphQLObjectType, GraphQLUnionType].
                                Used as a workaround for GraphQL's lack of support for
                                inheritance across "types" (i.e. non-interfaces).
                                The key-value pairs in the dict specify that the "key" type
                                is equivalent to the "value" type, i.e. that the GraphQL type or
                                interface in the key is the most-derived common supertype
                                of every GraphQL type in the "value" GraphQL union

    Returns:
        MergedSchemaDescriptor, a namedtuple that contains the AST of the merged schema,
        and the map from names of types/query type fields to the id of the schema that they
        came from. Scalars and directives will not appear in the map, as the same set of
        scalars and directives are expected to be defined in every schema.

    Raises:
        - ValueError if some schema identifier is not a nonempty string of alphanumeric
          characters and underscores, or if there are no more than one input schema to merge
        - SchemaStructureError if the schema does not have the expected form; in particular, if
          the AST does not represent a valid schema, if any query type field does not have the
          same name as the type that it queries, if the schema contains type extensions or
          input object definitions, or if the schema contains mutations or subscriptions
        - SchemaNameConflictError if there are conflicts between the names of
          types/interfaces/enums/scalars, conflicts between the names of fields (including
          fields created by cross-schema edges), or conflicts between the definition of
          directives with the same name
        - InvalidCrossSchemaEdgeError if some cross-schema edge provided lies within one schema,
          refers nonexistent schemas, types, fields, or connects non-scalar or non-matching
          fields
    """
    if len(schema_id_to_ast) <= 1:
        raise ValueError("Expected at least two schemas to merge.")

    query_type = "RootSchemaQuery"
    merged_schema_ast = _get_basic_schema_ast(query_type)  # Document

    type_name_to_schema_id = {}  # Dict[str, str], name of object/interface/enum/union to schema id
    scalars = {"String", "Int", "Float", "Boolean", "ID"}  # Set[str], user defined + builtins
    directives = {}  # Dict[str, DirectiveDefinition]

    for current_schema_id, current_ast in six.iteritems(schema_id_to_ast):
        current_ast = deepcopy(current_ast)
        merged_schema_ast, type_name_to_schema_id, scalars, directives = _accumulate_types(
            merged_schema_ast,
            query_type,
            type_name_to_schema_id,
            scalars,
            directives,
            current_schema_id,
            current_ast,
        )

    if type_equivalence_hints is None:
        type_equivalence_hints = {}
    merged_schema_ast = _add_cross_schema_edges(
        merged_schema_ast,
        type_name_to_schema_id,
        scalars,
        cross_schema_edges,
        type_equivalence_hints,
        query_type,
    )

    return MergedSchemaDescriptor(
        schema_ast=merged_schema_ast,
        schema=build_ast_schema(merged_schema_ast),
        type_name_to_schema_id=type_name_to_schema_id,
    )


def _get_basic_schema_ast(query_type):
    """Create a basic AST Document representing a nearly blank schema.

    The output AST contains a single query type, whose name is the input string. The query type
    is guaranteed to be the second entry of Document definitions, after the schema definition.
    The query type has no fields.

    Args:
        query_type: str, name of the query type for the schema

    Returns:
        DocumentNode, representing a nearly blank schema
    """
    blank_ast = ast_types.DocumentNode(
        definitions=[
            ast_types.SchemaDefinitionNode(
                operation_types=[
                    ast_types.OperationTypeDefinitionNode(
                        operation=ast_types.OperationType.QUERY,
                        type=ast_types.NamedTypeNode(name=ast_types.NameNode(value=query_type)),
                    )
                ],
                directives=[],
            ),
            ast_types.ObjectTypeDefinitionNode(
                name=ast_types.NameNode(value=query_type), fields=[], interfaces=[], directives=[],
            ),
        ]
    )
    return blank_ast


def _accumulate_types(
    merged_schema_ast,
    merged_query_type_name,
    type_name_to_schema_id,
    scalars,
    directives,
    current_schema_id,
    current_ast,
):
    """Add all types and query type fields of current_ast into merged_schema_ast.

    Args:
        merged_schema_ast: DocumentNode.
        merged_query_type_name: str, name of the query type in the merged_schema_ast
        type_name_to_schema_id: Dict[str, str], mapping type name to the id of the schema that
                                the type is from.
        scalars: Set[str], names of all scalars in the merged_schema so far.
        directives: Dict[str, DirectiveDefinitionNode], mapping directive name to definition.
        current_schema_id: str, identifier of the schema being merged
        current_ast: DocumentNode, representing the schema being merged into merged_schema_ast

    Returns:
        tuple (new_merged_schema_ast, type_name_to_schema_id, scalars, directives) with the
        following information:
            new_merged_schema_ast: DocumentNode, updated version of merged_schema_ast with
                                   current_ast incorporated.
            type_name_to_schema_id: Dict[str, str], updated version of type_name_to_schema_id input.
            scalars: Set[str], potentially updated version of scalars input.
            directives: Dict[str, DirectiveDefinitionNode], potentially updated version of
                        directives input.

    Raises:
        - ValueError if the schema identifier is not a nonempty string of alphanumeric
          characters and underscores
        - SchemaStructureError if the schema does not have the expected form; in particular, if
          the AST does not represent a valid schema, if any query type field does not have the
          same name as the type that it queries, if the schema contains type extensions or
          input object definitions, or if the schema contains mutations or subscriptions
        - SchemaNameConflictError if there are conflicts between the names of
          types/interfaces/enums/scalars, or conflicts between the definition of directives
          with the same name
    """
    # Check input schema identifier is a string of alphanumeric characters and underscores
    check_schema_identifier_is_valid(current_schema_id)
    # Check input schema satisfies various structural requirements
    check_ast_schema_is_valid(current_ast)

    current_schema = build_ast_schema(current_ast)
    current_query_type = get_query_type_name(current_schema)

    # Merge current_ast into merged_schema_ast.
    # Concatenate new scalars, new directives, and type definitions other than the query
    # type to definitions list.
    # Raise errors for conflicting scalars, directives, or types.
    new_definitions = current_ast.definitions  # List[Node]
    new_query_type_fields = None  # List[FieldDefinition]

    for new_definition in new_definitions:
        if isinstance(new_definition, ast_types.SchemaDefinitionNode):
            continue
        elif (
            isinstance(new_definition, ast_types.ObjectTypeDefinitionNode)
            and new_definition.name.value == current_query_type
        ):  # query type definition
            new_query_type_fields = new_definition.fields  # List[FieldDefinitionNode]
        elif isinstance(new_definition, ast_types.DirectiveDefinitionNode):
            directives, merged_schema_ast = _process_directive_definition(
                new_definition, directives, merged_schema_ast
            )
        elif isinstance(new_definition, ast_types.ScalarTypeDefinitionNode):
            scalars, merged_schema_ast = _process_scalar_definition(
                new_definition, scalars, type_name_to_schema_id, merged_schema_ast
            )
        elif isinstance(
            new_definition,
            (
                ast_types.EnumTypeDefinitionNode,
                ast_types.InterfaceTypeDefinitionNode,
                ast_types.ObjectTypeDefinitionNode,
                ast_types.UnionTypeDefinitionNode,
            ),
        ):
            type_name_to_schema_id, merged_schema_ast = _process_generic_type_definition(
                new_definition,
                current_schema_id,
                scalars,
                type_name_to_schema_id,
                merged_schema_ast,
            )
        else:  # All definition types should've been covered
            raise AssertionError(
                "Unreachable code reached. Missed definition type: "
                '"{}"'.format(type(new_definition).__name__)
            )

    # Concatenate all query type fields.
    # Since query_type was taken from the schema built from the input AST, the query type
    # should never be not found.
    if new_query_type_fields is None:
        raise AssertionError(
            'Unreachable code reached. Query type "{}" field definitions '
            "unexpectedly not found.".format(current_query_type)
        )

    # Note that as field names and type names have been confirmed to match up, and types
    # were merged without name conflicts, query type fields can also be safely merged.
    #
    # Query type is the second entry in the list of definitions of the merged_schema_ast,
    # as guaranteed by _get_basic_schema_ast()
    query_type_index = 1
    new_definitions = list(merged_schema_ast.definitions)
    merged_query_type_definition = new_definitions[query_type_index]
    if merged_query_type_definition.name.value != merged_query_type_name:
        raise AssertionError(
            "Unreachable code reached. The second definition in the schema is unexpectedly "
            'not the query type "{}", but is instead "{}".'.format(
                merged_query_type_name, merged_query_type_definition.name.value
            )
        )
    new_fields = list(merged_query_type_definition.fields)
    new_fields.extend(new_query_type_fields)
    new_merged_query_type_definition = ast_types.ObjectTypeDefinitionNode(
        name=merged_query_type_definition.name,
        interfaces=merged_query_type_definition.interfaces,
        fields=new_fields,
        directives=merged_query_type_definition.directives,
    )
    new_definitions[query_type_index] = new_merged_query_type_definition
    new_merged_schema_ast = ast_types.DocumentNode(definitions=new_definitions)
    return new_merged_schema_ast, type_name_to_schema_id, scalars, directives


def _process_directive_definition(directive, existing_directives, merged_schema_ast):
    """Compare new directive against existing directives, update records and schema.

    Args:
        directive: DirectiveDefinitionNode, an AST node representing the definition of a directive
        existing_directives: Dict[str, DirectiveDefinitionNode], mapping the name of each existing
                             directive to the AST node defining it.
        merged_schema_ast: DocumentNode, AST representing a schema.

    Returns:
        tuple (new_existing_directives, new_merged_schema_ast) with the following information:
            new_existing_directives: Dict[str, DirectiveDefinitionNode], existing_directives updated
                                     with the directive.
            new_merged_schema_ast: DocumentNode, merged_schema_ast with new directive added to its
                                   definitions.
    """
    directive_name = directive.name.value
    if directive_name in existing_directives:
        if print_ast(directive) == print_ast(existing_directives[directive_name]):
            return existing_directives, merged_schema_ast
        else:
            raise SchemaNameConflictError(
                'Directive "{}" with definition "{}" has already been defined with '
                'definition "{}".'.format(
                    directive_name,
                    print_ast(directive),
                    print_ast(existing_directives[directive_name]),
                )
            )
    # new directive
    new_definitions = list(merged_schema_ast.definitions)
    new_definitions.append(directive)
    new_merged_schema_ast = ast_types.DocumentNode(definitions=new_definitions)
    new_existing_directives = dict(existing_directives)
    new_existing_directives[directive_name] = directive
    return new_existing_directives, new_merged_schema_ast


def _process_scalar_definition(scalar, existing_scalars, type_name_to_schema_id, merged_schema_ast):
    """Compare new scalar against existing scalars and types, update records and schema.

    Args:
        scalar: ScalarDefinition, an AST node representing the definition of a scalar
        existing_scalars: Set[str], set of names of all existing scalars.
        type_name_to_schema_id: Dict[str, str], mapping names of types to the identifier of the
                                schema that they came from
        merged_schema_ast: DocumentNode, AST representing a schema.

    Returns:
        tuple (new_existing_scalars, new_merged_schema_ast) with the following information:
            new_existing_scalars: Set[str], existing_scalars updated with the name of the scalar
                                  added.
            new_merged_schema_ast: DocumentNode: merged_schema_ast with new scalar added to its
                                   definitions.
    """
    scalar_name = scalar.name.value
    if scalar_name in existing_scalars:
        return existing_scalars, merged_schema_ast
    if scalar_name in type_name_to_schema_id:
        raise SchemaNameConflictError(
            'New scalar "{}" clashes with existing type "{}" in schema "{}". Consider '
            'renaming type "{}" in schema "{}" using the tool rename_schema before merging '
            "to avoid conflicts.".format(
                scalar_name,
                scalar_name,
                type_name_to_schema_id[scalar_name],
                scalar_name,
                type_name_to_schema_id[scalar_name],
            )
        )
    # new, valid scalar
    new_definitions = list(merged_schema_ast.definitions)
    new_definitions.append(scalar)
    new_merged_schema_ast = ast_types.DocumentNode(definitions=new_definitions)
    new_existing_scalars = set(existing_scalars)
    new_existing_scalars.add(scalar_name)
    return new_existing_scalars, new_merged_schema_ast


def _process_generic_type_definition(
    generic_type, schema_id, existing_scalars, type_name_to_schema_id, merged_schema_ast
):
    """Compare new type against existing scalars and types, update records and schema.

    Args:
        generic_type: Any of EnumTypeDefinitionNode, InterfaceTypeDefinitionNode,
                      ObjectTypeDefinitionNode, or UnionTypeDefinitionNode, an AST node
                      representing the definition of a type.
        schema_id: str, the identifier of the schema that this type came from.
        existing_scalars: Set[str], set of names of all existing scalars.
        type_name_to_schema_id: Dict[str, str], mapping names of types to the identifier of the
                                schema that they came from.
        merged_schema_ast: DocumentNode, AST representing a schema.

    Returns:
        tuple (new_type_name_to_schema_id, new_merged_schema_ast) with the following information:
            new_type_name_to_schema_id: Dict[str, str], type_name_to_schema_id updated with the
                                        new generic_type.
            new_merged_schema_ast: DocumentNode, merged_schema_ast with new generic_type added to
                                   its definitions.
    """
    type_name = generic_type.name.value
    if type_name in existing_scalars:
        raise SchemaNameConflictError(
            'New type "{}" in schema "{}" clashes with existing scalar. Consider '
            'renaming type "{}" in schema "{}" using the tool rename_schema before merging '
            "to avoid conflicts.".format(type_name, schema_id, type_name, schema_id)
        )
    if type_name in type_name_to_schema_id:
        raise SchemaNameConflictError(
            'New type "{}" in schema "{}" clashes with existing type "{}" in schema "{}". '
            'Consider renaming type "{}" in either schema before merging to avoid '
            "conflicts.".format(
                type_name, schema_id, type_name, type_name_to_schema_id[type_name], type_name
            )
        )

    new_definitions = list(merged_schema_ast.definitions)
    new_definitions.append(generic_type)
    new_merged_schema_ast = ast_types.DocumentNode(definitions=new_definitions)
    new_type_name_to_schema_id = dict(type_name_to_schema_id)
    new_type_name_to_schema_id[type_name] = schema_id
    return new_type_name_to_schema_id, new_merged_schema_ast


def _add_cross_schema_edges(
    schema_ast,
    type_name_to_schema_id,
    scalars,
    cross_schema_edges,
    type_equivalence_hints,
    query_type,
):
    """Add cross-schema edges into the schema AST.

    Each cross-schema edge will be incorporated into the schema by adding vertex fields
    with a @stitch directive to relevant vertex types. The new fields corresponding to the
    added cross-schema edges will have names constructed from the edge name, prefixed with
    "out_" on the edge's outbound side, and "in_" on the edge's inbound side.

    The type of the new field will either be the type of the opposing vertex specified in
    the cross-schema edge, or the equivalent union type of the type of the opposing vertex
    if such a union type is specified by type_equivalence_hints.

    New vertex fields will be added to not only each vertex specified by the cross-schema
    edge, but to all of their subclass vertices as well.

    For examples demonstrating the above behaviors, see tests in test_merge_schemas.py that
    involve subclasses.

    Args:
        schema_ast: DocumentNode, representing a schema, satisfying various structural requirements
                    as demanded by `check_ast_schema_is_valid` in utils.py.
        type_name_to_schema_id: Dict[str, str], mapping type name to the id of the schema that
                                the type is from. Contains all Interface, Object, Union, and
                                Enum types
        scalars: Set[str], names of all scalars in the merged_schema so far
        cross_schema_edges: List[CrossSchemaEdgeDescriptor], containing all edges connecting
                            fields in multiple schemas to be added to the merged schema
        type_equivalence_hints: Dict[GraphQLObjectType, GraphQLUnionType].
                                Used as a workaround for GraphQL's lack of support for
                                inheritance across "types" (i.e. non-interfaces).
                                The key-value pairs in the dict specify that the "key" type
                                is equivalent to the "value" type, i.e. that the GraphQL type or
                                interface in the key is the most-derived common supertype
                                of every GraphQL type in the "value" GraphQL union
        query_type: str, name of the query type in the merged schema

    Returns:
        DocumentNode, representing the schema_ast with added edges from cross_schema_edges

    Raises:
        - SchemaNameConflictError if any cross-schema edge name causes a name conflict with
          existing fields, or with fields created by previous cross-schema edges
        - InvalidCrossSchemaEdgeError if any cross-schema edge lies within one schema, refers
          to nonexistent schemas, types, or fields, refers to Union types, stitches together
          fields that are not of a scalar type, or stitches together fields that are of
          different scalar types
    """
    # Build map of definitions for ease of modification
    type_name_to_definition = {}  # Dict[str, (Interface/Object)TypeDefinition]
    union_type_names = set()  # Set[str], contains names of union types, used for error messages

    for definition in schema_ast.definitions:
        if (
            isinstance(definition, ast_types.ObjectTypeDefinitionNode)
            and definition.name.value == query_type
        ):  # query type definition
            continue
        if isinstance(
            definition, (ast_types.InterfaceTypeDefinitionNode, ast_types.ObjectTypeDefinitionNode,)
        ):
            type_name_to_definition[definition.name.value] = definition
        elif isinstance(definition, (ast_types.UnionTypeDefinitionNode,)):
            union_type_names.add(definition.name.value)

    # NOTE: All merge_schemas needs is the dict mapping names to names, not the dict mapping
    # GraphQLObjects to GraphQLObjects. However, elsewhere in the repo, type_equivalence_hints
    # is a map of objects to objects, and thus we use that same input for consistency
    equivalent_type_names = {
        object_type.name: union_type.name
        for object_type, union_type in six.iteritems(type_equivalence_hints)
    }
    subclass_sets = compute_subclass_sets(build_ast_schema(schema_ast), type_equivalence_hints)

    # Iterate through edges list, incorporate each edge on one or both sides
    for cross_schema_edge in cross_schema_edges:
        _check_cross_schema_edge_is_valid(
            type_name_to_definition,
            type_name_to_schema_id,
            scalars,
            union_type_names,
            cross_schema_edge,
        )

        edge_name = cross_schema_edge.edge_name
        outbound_field_reference = cross_schema_edge.outbound_field_reference
        inbound_field_reference = cross_schema_edge.inbound_field_reference

        # Get name of the type referenced by the edges in either direction
        # This is equal to the sink side's equivalent union type if it has one
        outbound_edge_sink_type_name = equivalent_type_names.get(
            inbound_field_reference.type_name, inbound_field_reference.type_name
        )
        inbound_edge_sink_type_name = equivalent_type_names.get(
            outbound_field_reference.type_name, outbound_field_reference.type_name
        )

        # Get set of all the types that need the new edge field
        outbound_edge_source_type_names = subclass_sets[outbound_field_reference.type_name]
        for outbound_edge_source_type_name in outbound_edge_source_type_names:
            source_type_node = type_name_to_definition[outbound_edge_source_type_name]
            new_source_type_node = _add_edge_field(
                source_type_node,
                outbound_edge_sink_type_name,
                outbound_field_reference.field_name,
                inbound_field_reference.field_name,
                edge_name,
                OUTBOUND_EDGE_DIRECTION,
            )
            type_name_to_definition[outbound_edge_source_type_name] = new_source_type_node

        if not cross_schema_edge.out_edge_only:
            inbound_edge_source_type_names = subclass_sets[inbound_field_reference.type_name]
            for inbound_edge_source_type_name in inbound_edge_source_type_names:
                source_type_node = type_name_to_definition[inbound_edge_source_type_name]
                new_source_type_node = _add_edge_field(
                    source_type_node,
                    inbound_edge_sink_type_name,
                    inbound_field_reference.field_name,
                    outbound_field_reference.field_name,
                    edge_name,
                    INBOUND_EDGE_DIRECTION,
                )
                type_name_to_definition[inbound_edge_source_type_name] = new_source_type_node

    new_definitions = []
    for definition in schema_ast.definitions:
        if (
            isinstance(definition, ast_types.ObjectTypeDefinitionNode)
            and definition.name.value == query_type
        ):  # query type definition
            new_definitions.append(definition)
        elif isinstance(
            definition, (ast_types.InterfaceTypeDefinitionNode, ast_types.ObjectTypeDefinitionNode,)
        ):
            new_definitions.append(type_name_to_definition[definition.name.value])
        else:
            new_definitions.append(definition)

    return ast_types.DocumentNode(definitions=FrozenList(new_definitions))


def _check_cross_schema_edge_is_valid(
    type_name_to_definition, type_name_to_schema_id, scalars, union_type_names, cross_schema_edge
):
    """Check that the edge crosses schemas and has valid field references of correct types.

    Args:
        type_name_to_definition: Dict[str, (Interface/Object)TypeDefinition], mapping
                                 names of Interface and Object types to their definitions
        type_name_to_schema_id: Dict[str, str], mapping type name to the id of the schema that
                                the type is from. Contains not just Interface and Object type
                                definitions, but also Union and Enum types
        scalars: Set[str], names of all scalars in the merged_schema, including both built in
                 and user defined scalars
        union_type_names: Set[str], names of all union types in the merged schema, used for
                          informative error messages
        cross_schema_edge: CrossSchemaEdgeDescriptor namedtuple, the edge that we check the
                           validity of

    Raises:
        - InvalidCrossSchemaEdgeError if the cross-schema edge lies within one schema, refers
          to nonexistent schemas, types, or fields, refers to Union types, stitches together
          fields that are not of a scalar type, or stitches together fields that are of
          different scalar types
    """
    outbound_field_reference = cross_schema_edge.outbound_field_reference
    inbound_field_reference = cross_schema_edge.inbound_field_reference

    _check_field_reference_is_valid(
        type_name_to_definition, type_name_to_schema_id, union_type_names, outbound_field_reference
    )
    _check_field_reference_is_valid(
        type_name_to_definition, type_name_to_schema_id, union_type_names, inbound_field_reference
    )

    if outbound_field_reference.schema_id == inbound_field_reference.schema_id:  # not cross-schema
        raise InvalidCrossSchemaEdgeError(
            'Edge "{}" does not cross schemas. All CrossSchemaEdgeDescriptors provided must '
            "connect together types from different schemas.".format(cross_schema_edge)
        )

    _check_field_types_are_matching_scalars(type_name_to_definition, scalars, cross_schema_edge)


def _check_field_reference_is_valid(
    type_name_to_definition, type_name_to_schema_id, union_type_names, field_reference
):
    """Check that the field reference refers to a valid field.

    In particular, check that the field reference is on a type that exists in the correct
    schema, and that the type contains the field of the expected name.

    Args:
        type_name_to_definition: Dict[str, (Interface/Object)TypeDefinitionNode], mapping
                                 names of Interface and Object types to their definitions
        type_name_to_schema_id: Dict[str, str], mapping type name to the id of the schema that
                                the type is from. Contains not just Interface and Object type
                                definitions, but also Union and Enum types
        union_type_names: Set[str], names of all union types in the merged schema, used for
                          informative error messages
        field_reference: FieldReference namedtuple, what we check the validity of

    Raises:
        - InvalidCrossSchemaEdgeError if the cross-schema edge refers to nonexistent schemas,
          types, or fields, or refers to Union types
    """
    schema_id = field_reference.schema_id
    type_name = field_reference.type_name
    field_name = field_reference.field_name

    # Error if the type is a union, with suggestions on how to fix the problem
    if type_name in union_type_names:
        raise InvalidCrossSchemaEdgeError(
            'Type "{}" specified in the field reference "{}" is a union type, which may not '
            "be used in a cross-schema edge. Consider using the object type that is equivalent "
            "to this union type instead.".format(type_name, field_reference)
        )

    # Error if the type is nonexistent
    if type_name not in type_name_to_definition:
        raise InvalidCrossSchemaEdgeError(
            'Type "{}" specified in the field reference "{}" is not found '
            "in the merged schema.".format(type_name, field_reference)
        )

    # Error if the type is in a wrong or nonexistent schema
    if type_name_to_schema_id[type_name] != schema_id:
        raise InvalidCrossSchemaEdgeError(
            'Type "{}" specified in the field reference "{}" is expected to be in '
            'schema "{}", but is instead bound in schema "{}"'.format(
                type_name, field_reference, schema_id, type_name_to_schema_id[type_name]
            )
        )

    # Error if the type doesn't have the expected field
    type_definition = type_name_to_definition[type_name]
    type_fields = type_definition.fields
    if not any(field.name.value == field_name for field in type_fields):
        raise InvalidCrossSchemaEdgeError(
            'Field "{}" is not found under type "{}" in schema "{}", as expected by the '
            'field reference "{}".'.format(field_name, type_name, schema_id, field_reference)
        )


def _check_field_types_are_matching_scalars(type_name_to_definition, scalars, cross_schema_edge):
    """Check that stitched fields in a cross-schema edge are of the same scalar type.

    It is also legal for fields to be of a NonNull wrapped scalar type.

    Args:
        type_name_to_definition: Dict[str, (Interface/Object)TypeDefinitionNode], mapping
                                 name of types to their definitions
        scalars: Set[str], names of all scalars in the merged_schema, including both built in
                 and user defined scalars
        cross_schema_edge: CrossSchemaEdgeDescriptor namedtuple, the edge that we check the
                           validity of

    Raises:
        - InvalidCrossSchemaEdgeError if the cross-schema edge stitches together fields that are
          not of a scalar type, or stitched together fields that are of different scalar types
    """
    field_type_names = []

    for direction, field_reference in (
        (OUTBOUND_EDGE_DIRECTION, cross_schema_edge.outbound_field_reference),
        (INBOUND_EDGE_DIRECTION, cross_schema_edge.inbound_field_reference),
    ):
        type_name = field_reference.type_name
        field_name = field_reference.field_name

        fields = type_name_to_definition[type_name].fields  # List[FieldDefinition]
        field_type = None
        for field in fields:
            if field.name.value == field_name:
                field_type = get_ast_with_non_null_stripped(field.type)
                break

        if field_type is None:  # should never happen after _check_field_reference_is_valid
            raise AssertionError(
                'Unreachable code reached. Field "{}" unexpectedly ' "not found.".format(field_name)
            )

        if isinstance(field_type, ast_types.ListTypeNode):
            raise InvalidCrossSchemaEdgeError(
                'The {}bound field of cross-schema edge "{}" gives a list, while it '
                "should be a single scalar".format(direction, cross_schema_edge)
            )
        elif isinstance(field_type, ast_types.NamedTypeNode):
            if field_type.name.value not in scalars:
                raise InvalidCrossSchemaEdgeError(
                    'The {}bound field of cross-schema edge "{}" is of type "{}", which '
                    "is not a scalar".format(direction, cross_schema_edge, field_type.name.value)
                )
        else:  # since NonNull is stripped, field_type can only be ListType or NamedType
            raise AssertionError(
                "Unreachable code reached. Field has missed "
                'type "{}"'.format(type(field_type).__name__)
            )

        field_type_names.append(field_type.name.value)

    outbound_field_type_name, inbound_field_type_name = field_type_names
    if not _scalars_match(outbound_field_type_name, inbound_field_type_name):
        raise InvalidCrossSchemaEdgeError(
            'The outbound and inbound fields of edge "{}" are of different types, '
            '"{}" and "{}". They are expected to be of the same scalar type.'.format(
                cross_schema_edge, outbound_field_type_name, inbound_field_type_name
            )
        )


def _scalars_match(scalar_name1, scalar_name2):
    """Return whether the two input scalars are considered to be the same.

    For now, two input scalars are considered to be the same if they're the same scalar, if
    one is ID and the other is String, or if one is ID and the other is Int. This may be
    extended in the future.

    Args:
        scalar_name1: str, name of a scalar, may be built-in or user defined
        scalar_name2: str, name of a scalar, may be built-in or user defined

    Returns:
        True if the two scalars are considered the same, False otherwise
    """
    if scalar_name1 == scalar_name2:
        return True
    scalar_names = frozenset((scalar_name1, scalar_name2))
    if scalar_names == frozenset(("String", "ID")) or scalar_names == frozenset(("Int", "ID")):
        return True
    return False


def _add_edge_field(
    source_type_node, sink_type_name, source_field_name, sink_field_name, edge_name, direction
):
    """Add one direction of the specified edge as a field of the source type.

    Args:
        source_type_node: (Interface/Object)TypeDefinitionNode, where a new field representing
                          one direction of the edge will be added.
        sink_type_name: str, name of the type that the edge leads to
        source_field_name: str, name of the source side field that will be stitched
        sink_field_name: str, name of the sink side field that will be stitched
        edge_name: str, name of the edge that will be used to name the new field
        direction: str, either OUTBOUND_EDGE_DIRECTION or INBOUND_EDGE_DIRECTION ('out'
                   or 'in')

    Returns:
        (Interface/Object)TypeDefinitionNode, updated version of source_type_node.

    Raises:
        - SchemaNameConflictError if the new cross-schema edge name causes a name conflict with
          existing fields, or fields created by previous cross-schema edges
    """
    type_fields = source_type_node.fields

    if direction not in (OUTBOUND_EDGE_DIRECTION, INBOUND_EDGE_DIRECTION):
        raise AssertionError(
            'Input "direction" must be either "{}" or "{}".'.format(
                OUTBOUND_EDGE_DIRECTION, INBOUND_EDGE_DIRECTION
            )
        )
    new_edge_field_name = direction + "_" + edge_name

    # Error if new edge causes a field name clash
    if any(field.name.value == new_edge_field_name for field in type_fields):
        raise SchemaNameConflictError(
            'New field "{}" under type "{}" created by the {}bound field of edge named '
            '"{}" clashes with an existing field of the same name. Consider changing the '
            "name of your edge to avoid name conflicts.".format(
                new_edge_field_name, source_type_node.name.value, direction, edge_name
            )
        )

    new_edge_field_node = ast_types.FieldDefinitionNode(
        name=ast_types.NameNode(value=new_edge_field_name),
        arguments=[],
        type=ast_types.ListTypeNode(
            type=ast_types.NamedTypeNode(name=ast_types.NameNode(value=sink_type_name),),
        ),
        directives=[_build_stitch_directive(source_field_name, sink_field_name),],
    )

    new_type_fields = list(type_fields)
    new_type_fields.append(new_edge_field_node)
    if type(source_type_node) == ast_types.ObjectTypeDefinitionNode:
        new_source_type_node = ast_types.ObjectTypeDefinitionNode(
            description=source_type_node.description,
            name=source_type_node.name,
            directives=source_type_node.directives,
            fields=new_type_fields,
            interfaces=source_type_node.interfaces,
        )
    elif type(source_type_node) == ast_types.InterfaceTypeDefinitionNode:
        new_source_type_node = ast_types.InterfaceTypeDefinitionNode(
            description=source_type_node.description,
            name=source_type_node.name,
            directives=source_type_node.directives,
            fields=new_type_fields,
        )
    else:
        raise AssertionError(
            'Input "source_type_node" must be of type {} or {}. Received type {}'.format(
                ast_types.ObjectTypeDefinitionNode,
                ast_types.InterfaceTypeDefinitionNode,
                type(source_type_node),
            )
        )
    return new_source_type_node


def _build_stitch_directive(source_field_name, sink_field_name):
    """Build a Directive node for the stitch directive."""
    return ast_types.DirectiveNode(
        name=ast_types.NameNode(value="stitch"),
        arguments=[
            ast_types.ArgumentNode(
                name=ast_types.NameNode(value="source_field"),
                value=ast_types.StringValueNode(value=source_field_name),
            ),
            ast_types.ArgumentNode(
                name=ast_types.NameNode(value="sink_field"),
                value=ast_types.StringValueNode(value=sink_field_name),
            ),
        ],
    )
