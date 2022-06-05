import json

from django import template
from django.apps import apps
from django.core.cache import cache
from django.utils.safestring import mark_safe
# from django.template.loader_tags import do_include

from django_generic_view_extensions.model import object_in_list_format, field_render
from django_generic_view_extensions.util import numeric_if_possible

from django_cache_memoized import memoized

from ..models import APP

register = template.Library()


@register.simple_tag
def setvar(val=None):
    '''
    Used as follows:

    {% setvar "value" as variable_name %}

    and then applied with {{variable_name}}.

    :param val: a value to set the variable to.
    '''
    return val


@register.simple_tag(takes_context=True)
def list_format(context, obj):
    '''

    :param context:
    :param obj:
    '''
    return object_in_list_format(obj, context)


@register.simple_tag()
def get_list(form_data, model, attribute):
    '''
    Given a form.data dictionary whichcontains formset data extract and return a list of the
    values of the attributes in the named model from the forms in that formset. This is used
    when a form with related_forms fails validation and bounces back, we need to pump the formset
    data back into the form, and if the formsets are constructed in Javascript they may like
    lists of values such as produced here.

    :param form_data:
    :param model:
    :param attribute:
    '''
    count = int(form_data.get(f"{model}-TOTAL_FORMS", 0))

    attr_list = []
    for i in range(count):
        key = f"{model}-{i}-{attribute}"
        val = form_data.get(key, None)
        val = numeric_if_possible(val)
        attr_list.append(val)

        # if not attr_list: breakpoint()
    return json.dumps(attr_list)


@register.simple_tag()
def leaderboard_before_rebuild(rebuild_log, game):
    '''
    :param rebuild_log: an instance of RebuildLog
    :param game: an instance of Game:
    '''
    return mark_safe(rebuild_log.leaderboard_before(game))


@register.simple_tag()
def leaderboard_after_rebuild(rebuild_log, game):
    '''
    :param rebuild_log: an instance of RebuildLog
    :param game: an instance of Game:
    '''
    return mark_safe(rebuild_log.leaderboard_after(game))


@register.simple_tag()
@memoized("{model}[{pk}]({link},{fmt})")
def field_str(model, pk, link=None, fmt=None):
    '''

    See: https://docs.djangoproject.com/en/4.0/topics/cache/#the-low-level-cache-api

    :param model: The name of a model
    :param pk: A primary key value for that model (object id)
    :param attribute: The name of an attribute of such an object (a model field or method)
    '''
    Model = apps.get_model(APP, model)
    obj = Model.objects.get(pk=pk)
    return field_render(obj, link, fmt)


@register.simple_tag()
@memoized("{model}[{pk}].{attribute}")
def get_attr(model, pk, attribute):
    '''

    See: https://docs.djangoproject.com/en/4.0/topics/cache/#the-low-level-cache-api

    :param model: The name of a model
    :param pk: A primary key value for that model (object id)
    :param attribute: The name of an attribute of such an object (a model field or method)
    '''
    Model = apps.get_model(APP, model)
    obj = Model.objects.get(pk=pk)
    attr = getattr(obj, attribute)
    if callable(attr):
        return mark_safe(attr())
    else:
        return mark_safe(attr)
