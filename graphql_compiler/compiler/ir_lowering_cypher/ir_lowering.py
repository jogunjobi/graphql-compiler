# Copyright 2019-present Kensho Technologies, LLC.
from functools import partial

from ..blocks import (
    Backtrack, CoerceType, Filter, Fold, GlobalOperationsStart, MarkLocation, QueryRoot, Recurse,
    Traverse
)
from ..expressions import ContextField, FoldedContextField, LocalField
from ..ir_lowering_common.location_renaming import (
    make_location_rewriter_visitor_fn, make_revisit_location_translations
)
from ..helpers import FoldScopeLocation

##################################
# Optimization / lowering passes #
##################################

def insert_explicit_type_bounds(ir_blocks, query_metadata_table, type_equivalence_hints=None):
    """Add a CoerceType block after every Traverse/Fold/Recurse, to hint to the Cypher scheduler."""
    # Cypher might not be aware of the fact that all our edges' endpoints are strictly typed,
    # so we expose the implicit types of edges' endpoints explicitly, by adding CoerceType blocks.
    new_ir_blocks = []

    for current_index, block in enumerate(ir_blocks):
        new_ir_blocks.append(block)

        if isinstance(block, (Traverse, Fold, Recurse)):
            # We need to add an explicit CoerceType immediately after this block, if one is not
            # already present. If one is present, we do nothing. Since filtering happens before
            # location-marking, if we find a MarkLocation without finding a CoerceType, we know
            # there is no CoerceType here.
            #
            # In that case, we look up that location's type in the query metadata table,
            # and make a new CoerceType block before continuing.
            next_mark_location = None
            next_coerce_type = None
            lookup_index = current_index + 1
            while lookup_index < len(ir_blocks):
                lookup_block = ir_blocks[lookup_index]
                if isinstance(lookup_block, CoerceType):
                    next_coerce_type = lookup_block
                    break
                elif isinstance(lookup_block, MarkLocation):
                    next_mark_location = lookup_block
                    break
                elif isinstance(lookup_block, Filter):
                    # This is expected, step over it.
                    lookup_index += 1
                else:
                    raise AssertionError(u'Expected only CoerceType and Filter blocks to appear '
                                         u'between {} and the corresponding MarkLocation, but '
                                         u'unexpectedly found {}. IR blocks: {}'
                                         .format(block, lookup_block, ir_blocks))

            if next_coerce_type:
                # There's already a type coercion here, nothing needs to be done here.
                pass
            elif next_mark_location:
                location_info = query_metadata_table.get_location_info(next_mark_location.location)
                new_ir_blocks.append(CoerceType({location_info.type.name}))
            else:
                raise AssertionError(u'Illegal IR blocks found. Block {} at index {} does not have '
                                     u'a MarkLocation or CoerceType block after it: {}'
                                     .format(block, current_index, ir_blocks))

    return new_ir_blocks


def remove_mark_location_after_optional_backtrack(ir_blocks, query_metadata_table):
    """Remove location revisits, since they are not required in Cypher."""
    location_translations = make_revisit_location_translations(query_metadata_table)
    visitor_fn = make_location_rewriter_visitor_fn(location_translations)

    new_ir_blocks = []
    for block in ir_blocks:
        if isinstance(block, MarkLocation) and block.location in location_translations:
            # Drop this block, since we'll be replacing its location with its revisit origin.
            pass
        else:
            # Rewrite the locations in this block (if any), to reflect the desired translation.
            new_block = block.visit_and_update_expressions(visitor_fn)
            new_ir_blocks.append(new_block)

    return new_ir_blocks


def replace_local_fields_with_context_fields(ir_blocks):
    """Rewrite LocalField expressions into ContextField expressions referencing that location."""
    def visitor_func_base(location, expression):
        """Rewriter function that converts LocalFields into ContextFields at the given location."""
        if not isinstance(expression, LocalField):
            return expression

        location_at_field = location.navigate_to_field(expression.field_name)
        if isinstance(location, FoldScopeLocation):
            return FoldedContextField(location_at_field, expression.field_type)
        else:
            return ContextField(location_at_field, expression.field_type)

    new_ir_blocks = []
    blocks_to_be_rewritten = []
    for block in ir_blocks:
        if isinstance(block, MarkLocation):
            # First, rewrite all the blocks that might have referenced this location.
            visitor_fn = partial(visitor_func_base, block.location)
            for block_for_rewriting in blocks_to_be_rewritten:
                new_block = block_for_rewriting.visit_and_update_expressions(visitor_fn)
                new_ir_blocks.append(new_block)

            # Then, append the MarkLocation block itself and start with an empty rewrite list.
            blocks_to_be_rewritten = []
            new_ir_blocks.append(block)
        else:
            blocks_to_be_rewritten.append(block)

    # Append any remaining blocks that did not need rewriting.
    new_ir_blocks.extend(blocks_to_be_rewritten)

    if len(ir_blocks) != len(new_ir_blocks):
        raise AssertionError(u'The number of IR blocks unexpectedly changed, {} vs {}: {} {}'
                             .format(len(ir_blocks), len(new_ir_blocks), ir_blocks, new_ir_blocks))

    return new_ir_blocks


def move_filters_in_optional_locations_to_global_operations(ir_blocks, query_metadata_table):
    """Move Filter blocks found within @optional traversals to the global operations section.

    This transformation is necessary to uphold the compiler's chosen semantics around filters
    within optional traversals: if the edge exists but the filter fails to match, we can't pretend
    the edge didn't exist. The Cypher specification chooses the opposite approach. The
    transformation implemented here allows us to address this inconsistency: we apply the
    "optional edge" semantics, then materialize the result, and *then* apply the filters that
    applied to any locations within optional traversals.

    This function assumes that all LocalField expressions have been suitably replaced with ones
    that explicitly reference the context of the field (whether folded or not).

    Args:
        ir_blocks: list of IR blocks to rewrite
        query_metadata_table: QueryMetadataTable object that captures information about the query

    Returns:
        list of IR blocks with all Filter blocks affecting optional traversals moved to after the
        GlobalOperationsStart block
    """
    new_ir_blocks = []
    global_filters = []
    within_optional_scope = False
    for block in ir_blocks:
        new_block = block

        if within_optional_scope and isinstance(block, Filter):
            global_filters.append(block)
            new_block = None  # Do not append the block in its original location.
        elif isinstance(block, Traverse) and block.optional:
            within_optional_scope = True
        elif isinstance(block, Backtrack):
            location_info = query_metadata_table.get_location_info(block.location)
            within_optional_scope = location_info.optional_scopes_depth > 0
        else:
            # No changes or special behaviors for any other blocks.
            pass

        if new_block is not None:
            new_ir_blocks.append(new_block)

        if isinstance(block, GlobalOperationsStart):
            # We just appended the block that signals the start of the global operations section.
            # This is where all the global filters go.
            new_ir_blocks.extend(global_filters)
            global_filters = []

    return new_ir_blocks
