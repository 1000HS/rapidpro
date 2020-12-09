import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import iso8601
import regex
import requests
from packaging.version import Version
from smartmin.views import (
    SmartCreateView,
    SmartCRUDL,
    SmartDeleteView,
    SmartFormView,
    SmartListView,
    SmartReadView,
    SmartTemplateView,
    SmartUpdateView,
    smart_url,
)

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Max, Min, Sum
from django.db.models.functions import Lower
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils.encoding import force_text
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import FormView

from temba import mailroom
from temba.archives.models import Archive
from temba.channels.models import Channel
from temba.contacts.models import URN, ContactField, ContactGroup
from temba.contacts.search import SearchException, parse_query
from temba.contacts.search.omnibox import omnibox_deserialize
from temba.flows.models import Flow, FlowRevision, FlowRun, FlowRunCount, FlowSession, FlowStart
from temba.flows.tasks import export_flow_results_task
from temba.ivr.models import IVRCall
from temba.mailroom import FlowValidationException
from temba.orgs.models import Org
from temba.orgs.views import ModalMixin, OrgObjPermsMixin, OrgPermsMixin
from temba.templates.models import Template
from temba.triggers.models import Trigger
from temba.utils import analytics, gettext, json, on_transaction_commit, str_to_bool
from temba.utils.fields import (
    CheckboxWidget,
    ContactSearchWidget,
    InputWidget,
    JSONField,
    OmniboxChoice,
    SelectMultipleWidget,
    SelectWidget,
)
from temba.utils.s3 import public_file_storage
from temba.utils.text import slugify_with
from temba.utils.uuid import uuid4
from temba.utils.views import BulkActionMixin

from .models import (
    ExportFlowResultsTask,
    FlowLabel,
    FlowPathRecentRun,
    FlowStartCount,
    FlowUserConflictException,
    FlowVersionConflictException,
)

logger = logging.getLogger(__name__)


EXPIRES_CHOICES = (
    (5, _("After 5 minutes")),
    (10, _("After 10 minutes")),
    (15, _("After 15 minutes")),
    (30, _("After 30 minutes")),
    (60, _("After 1 hour")),
    (60 * 3, _("After 3 hours")),
    (60 * 6, _("After 6 hours")),
    (60 * 12, _("After 12 hours")),
    (60 * 18, _("After 18 hours")),
    (60 * 24, _("After 1 day")),
    (60 * 24 * 2, _("After 2 days")),
    (60 * 24 * 3, _("After 3 days")),
    (60 * 24 * 7, _("After 1 week")),
    (60 * 24 * 14, _("After 2 weeks")),
    (60 * 24 * 30, _("After 30 days")),
)


class OrgQuerysetMixin:
    def derive_queryset(self, *args, **kwargs):
        queryset = super().derive_queryset(*args, **kwargs)
        if not self.request.user.is_authenticated:  # pragma: needs cover
            return queryset.exclude(pk__gt=0)
        else:
            return queryset.filter(org=self.request.user.get_org())


class BaseFlowForm(forms.ModelForm):
    def clean_keyword_triggers(self):
        org = self.user.get_org()
        value = self.data.getlist("keyword_triggers", [])

        duplicates = []
        wrong_format = []
        cleaned_keywords = []

        for keyword in value:
            keyword = keyword.lower().strip()
            if not keyword:  # pragma: needs cover
                continue

            if (
                not regex.match(r"^\w+$", keyword, flags=regex.UNICODE | regex.V0)
                or len(keyword) > Trigger.KEYWORD_MAX_LEN
            ):
                wrong_format.append(keyword)

            # make sure it is unique on this org
            existing = Trigger.objects.filter(org=org, keyword__iexact=keyword, is_archived=False, is_active=True)
            if self.instance:
                existing = existing.exclude(flow=self.instance.pk)

            if existing:
                duplicates.append(keyword)
            else:
                cleaned_keywords.append(keyword)

        if wrong_format:
            raise forms.ValidationError(
                _(
                    '"%(keyword)s" must be a single word, less than %(limit)d characters, containing only letter '
                    "and numbers"
                )
                % dict(keyword=", ".join(wrong_format), limit=Trigger.KEYWORD_MAX_LEN)
            )

        if duplicates:
            if len(duplicates) > 1:
                error_message = _('The keywords "%s" are already used for another flow') % ", ".join(duplicates)
            else:
                error_message = _('The keyword "%s" is already used for another flow') % ", ".join(duplicates)
            raise forms.ValidationError(error_message)

        return ",".join(cleaned_keywords)

    class Meta:
        model = Flow
        fields = "__all__"


class PartialTemplate(SmartTemplateView):  # pragma: no cover
    def pre_process(self, request, *args, **kwargs):
        self.template = kwargs["template"]
        return

    def get_template_names(self):
        return "partials/%s.html" % self.template


class FlowSessionCRUDL(SmartCRUDL):
    actions = ("json",)
    model = FlowSession

    class Json(SmartReadView):
        slug_url_kwarg = "uuid"
        permission = "flows.flowsession_json"

        def get(self, request, *args, **kwargs):
            session = self.get_object()
            output = session.output
            output["_metadata"] = dict(
                session_id=session.id, org=session.org.name, org_id=session.org_id, site=self.request.branding["link"]
            )
            return JsonResponse(output, json_dumps_params=dict(indent=2))


class FlowRunCRUDL(SmartCRUDL):
    actions = ("delete",)
    model = FlowRun

    class Delete(ModalMixin, OrgObjPermsMixin, SmartDeleteView):
        fields = ("pk",)
        success_message = None

        def post(self, request, *args, **kwargs):
            self.get_object().release(FlowRun.DELETE_FOR_USER)
            return HttpResponse()


class FlowCRUDL(SmartCRUDL):
    actions = (
        "list",
        "archived",
        "copy",
        "create",
        "delete",
        "update",
        "simulate",
        "change_language",
        "export_translation",
        "download_translation",
        "import_translation",
        "export_results",
        "upload_action_recording",
        "editor",
        "editor_next",
        "results",
        "run_table",
        "category_counts",
        "broadcast",
        "activity",
        "activity_chart",
        "filter",
        "campaign",
        "revisions",
        "recent_messages",
        "assets",
        "upload_media_action",
    )

    model = Flow

    class AllowOnlyActiveFlowMixin(object):
        def get_queryset(self):
            initial_queryset = super().get_queryset()
            return initial_queryset.filter(is_active=True)

    class RecentMessages(OrgObjPermsMixin, SmartReadView):
        """
        Used by the editor for the rollover of recent messages on path segments in a flow
        """

        slug_url_kwarg = "uuid"

        def get(self, request, *args, **kwargs):
            exit_uuids = request.GET.get("exits", "").split(",")
            to_uuid = request.GET.get("to")

            recent_messages = []

            if exit_uuids and to_uuid:
                for recent_run in FlowPathRecentRun.get_recent(exit_uuids, to_uuid):
                    recent_messages.append(
                        {"sent": json.encode_datetime(recent_run["visited_on"]), "text": recent_run["text"]}
                    )

            return JsonResponse(recent_messages, safe=False)

    class Revisions(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        """
        Used by the editor for fetching and saving flow definitions
        """

        slug_url_kwarg = "uuid"

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r"^%s/%s/(?P<uuid>[0-9a-f-]+)/((?P<revision_id>\d+)/)?$" % (path, action)

        def get(self, request, *args, **kwargs):
            flow = self.get_object()
            revision_id = self.kwargs["revision_id"]

            # the editor requests the spec version it supports which allows us to add support for new versions
            # on the goflow/mailroom side before updating the editor to use that new version
            requested_version = request.GET.get("version", Flow.CURRENT_SPEC_VERSION)

            # we are looking for a specific revision, fetch it and migrate it forward
            if revision_id:
                revision = FlowRevision.objects.get(flow=flow, id=revision_id)
                definition = revision.get_migrated_definition(to_version=requested_version)

                # get our metadata
                flow_info = mailroom.get_client().flow_inspect(flow.org_id, definition)
                return JsonResponse(dict(definition=definition, metadata=Flow.get_metadata(flow_info)))

            # build a list of valid revisions to display
            revisions = []

            for revision in flow.revisions.all().order_by("-revision")[:100]:
                revision_version = Version(revision.spec_version)

                # our goflow revisions are already validated
                if revision_version >= Version(Flow.INITIAL_GOFLOW_VERSION):
                    revisions.append(revision.as_json())
                    continue

                # legacy revisions should be validated first as a failsafe
                try:
                    legacy_flow_def = revision.get_migrated_definition(to_version=Flow.FINAL_LEGACY_VERSION)
                    FlowRevision.validate_legacy_definition(legacy_flow_def)
                    revisions.append(revision.as_json())

                except ValueError:
                    # "expected" error in the def, silently cull it
                    pass

                except Exception as e:
                    # something else, we still cull, but report it to sentry
                    logger.error(
                        f"Error validating flow revision ({flow.uuid} [{revision.id}]): {str(e)}", exc_info=True
                    )
                    pass

            return JsonResponse({"results": revisions}, safe=False)

        def post(self, request, *args, **kwargs):
            if not self.has_org_perm("flows.flow_update"):
                return JsonResponse(
                    {"status": "failure", "description": _("You don't have permission to edit this flow")}, status=403
                )

            # try to parse our body
            definition = json.loads(force_text(request.body))
            try:
                flow = self.get_object(self.get_queryset())
                revision = flow.save_revision(self.request.user, definition)
                return JsonResponse(
                    {
                        "status": "success",
                        "saved_on": json.encode_datetime(flow.saved_on, micros=True),
                        "revision": revision.as_json(),
                        "metadata": flow.metadata,
                    }
                )

            except FlowValidationException as e:
                error = _("Your flow failed validation. Please refresh your browser.")
                detail = str(e)
            except FlowVersionConflictException:
                error = _(
                    "Your flow has been upgraded to the latest version. "
                    "In order to continue editing, please refresh your browser."
                )
                detail = None
            except FlowUserConflictException as e:
                error = (
                    _(
                        "%s is currently editing this Flow. "
                        "Your changes will not be saved until you refresh your browser."
                    )
                    % e.other_user
                )
                detail = None
            except Exception as e:  # pragma: no cover
                import traceback

                traceback.print_stack(e)
                error = _("Your flow could not be saved. Please refresh your browser.")
                detail = None

            return JsonResponse({"status": "failure", "description": error, "detail": detail}, status=400)

    class Create(ModalMixin, OrgPermsMixin, SmartCreateView):
        class FlowCreateForm(BaseFlowForm):
            keyword_triggers = forms.CharField(
                required=False,
                label=_("Global keyword triggers"),
                help_text=_("When a user sends any of these keywords they will begin this flow"),
                widget=SelectWidget(
                    attrs={
                        "widget_only": False,
                        "multi": True,
                        "searchable": True,
                        "tags": True,
                        "space_select": True,
                        "placeholder": _("Select keywords to trigger this flow"),
                    }
                ),
            )

            flow_type = forms.ChoiceField(
                label=_("Type"),
                help_text=_("Choose the method for your flow"),
                choices=(
                    (Flow.TYPE_MESSAGE, "Messaging"),
                    (Flow.TYPE_VOICE, "Phone Call"),
                    (Flow.TYPE_SURVEY, "Surveyor"),
                ),
                widget=SelectWidget(attrs={"widget_only": False}),
            )

            def __init__(self, user, branding, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.user = user

                org_languages = self.user.get_org().languages.all().order_by("orgs", "name")
                language_choices = ((lang.iso_code, lang.name) for lang in org_languages)

                flow_types = branding.get("flow_types", [Flow.TYPE_MESSAGE, Flow.TYPE_VOICE, Flow.TYPE_SURVEY])

                # prune our choices by brand config
                choices = []
                for flow_choice in self.fields["flow_type"].choices:
                    if flow_choice[0] in flow_types:
                        choices.append(flow_choice)
                self.fields["flow_type"].choices = choices

                self.fields["base_language"] = forms.ChoiceField(
                    label=_("Language"),
                    initial=self.user.get_org().primary_language,
                    choices=language_choices,
                    widget=SelectWidget(attrs={"widget_only": False}),
                )

            class Meta:
                model = Flow
                fields = ("name", "keyword_triggers", "flow_type", "base_language")
                widgets = {"name": InputWidget()}

        form_class = FlowCreateForm
        success_url = "uuid@flows.flow_editor"
        success_message = ""
        field_config = dict(name=dict(help=_("Choose a name to describe this flow, e.g. Demographic Survey")))

        def derive_exclude(self):
            user = self.request.user
            org = user.get_org()
            exclude = []

            if not org.primary_language:
                exclude.append("base_language")

            return exclude

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            kwargs["branding"] = self.request.branding
            return kwargs

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["has_flows"] = Flow.objects.filter(org=self.request.user.get_org(), is_active=True).count() > 0
            return context

        def save(self, obj):
            analytics.track(self.request.user.username, "temba.flow_created", dict(name=obj.name))
            org = self.request.user.get_org()

            # if we don't have a language, use base
            if not obj.base_language:  # pragma: needs cover
                obj.base_language = "base"

            # default expiration is a week
            expires_after_minutes = Flow.DEFAULT_EXPIRES_AFTER
            if obj.flow_type == Flow.TYPE_VOICE:
                # ivr expires after 5 minutes of inactivity
                expires_after_minutes = 5

            self.object = Flow.create(
                org,
                self.request.user,
                obj.name,
                flow_type=obj.flow_type,
                expires_after_minutes=expires_after_minutes,
                base_language=obj.base_language,
                create_revision=True,
            )

        def post_save(self, obj):
            user = self.request.user
            org = user.get_org()

            # create triggers for this flow only if there are keywords and we aren't a survey
            if self.form.cleaned_data.get("flow_type") != Flow.TYPE_SURVEY:
                if len(self.form.cleaned_data["keyword_triggers"]) > 0:
                    for keyword in self.form.cleaned_data["keyword_triggers"].split(","):
                        Trigger.objects.create(org=org, keyword=keyword, flow=obj, created_by=user, modified_by=user)

            return obj

    class Delete(AllowOnlyActiveFlowMixin, ModalMixin, OrgObjPermsMixin, SmartDeleteView):
        fields = ("id",)
        cancel_url = "uuid@flows.flow_editor"
        success_message = ""
        submit_button_name = _("Delete")

        def get_success_url(self):
            return reverse("flows.flow_list")

        def post(self, request, *args, **kwargs):
            flow = self.get_object()
            self.object = flow

            flows = Flow.objects.filter(org=flow.org, flow_dependencies__in=[flow])
            if flows.count():
                return HttpResponseRedirect(smart_url(self.cancel_url, flow))

            # do the actual deletion
            flow.release()

            # we can't just redirect so as to make our modal do the right thing
            response = self.render_to_response(
                self.get_context_data(
                    success_url=self.get_success_url(), success_script=getattr(self, "success_script", None)
                )
            )
            response["Temba-Success"] = self.get_success_url()

            return response

    class Copy(OrgObjPermsMixin, SmartUpdateView):
        fields = []
        success_message = ""

        def form_valid(self, form):
            # copy our current object
            copy = Flow.copy(self.object, self.request.user)

            # redirect to the newly created flow
            return HttpResponseRedirect(reverse("flows.flow_editor", args=[copy.uuid]))

    class Update(AllowOnlyActiveFlowMixin, ModalMixin, OrgObjPermsMixin, SmartUpdateView):
        class BaseUpdateFlowFormMixin:
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

                # if we don't have a base language let them pick one (this is immutable)
                if not self.instance.base_language:
                    choices = [("base", "No Preference")]
                    choices += [
                        (lang.iso_code, lang.name)
                        for lang in self.instance.org.languages.all().order_by("orgs", "name")
                    ]
                    self.fields["base_language"] = forms.ChoiceField(label=_("Language"), choices=choices)

            class Meta:
                widgets = {"name": InputWidget(), "ignore_triggers": CheckboxWidget()}

        class SurveyFlowUpdateForm(BaseUpdateFlowFormMixin, BaseFlowForm):
            expires_after_minutes = forms.ChoiceField(
                label=_("Expire inactive contacts"),
                help_text=_("When inactive contacts should be removed from the flow"),
                initial=str(60 * 24 * 7),
                choices=EXPIRES_CHOICES,
                widget=SelectWidget(attrs={"widget_only": False}),
            )
            contact_creation = forms.ChoiceField(
                label=_("Create a contact "),
                help_text=_("Whether surveyor logins should be used as the contact for each run"),
                choices=((Flow.CONTACT_PER_RUN, _("For each run")), (Flow.CONTACT_PER_LOGIN, _("For each login"))),
                widget=SelectWidget(attrs={"widget_only": False}),
            )

            def __init__(self, user, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.user = user

                metadata = self.instance.metadata

                contact_creation = self.fields["contact_creation"]
                contact_creation.initial = metadata.get(Flow.CONTACT_CREATION, Flow.CONTACT_PER_RUN)

            class Meta:
                model = Flow
                fields = ("name", "contact_creation", "expires_after_minutes")
                widgets = {"name": InputWidget()}

        class IVRFlowUpdateForm(BaseUpdateFlowFormMixin, BaseFlowForm):
            ivr_retry = forms.ChoiceField(
                label=_("Retry call if unable to connect"),
                help_text=_("Retries call three times for the chosen interval"),
                initial=60,
                choices=IVRCall.IVR_RETRY_CHOICES,
                widget=SelectWidget(attrs={"widget_only": False}),
            )
            expires_after_minutes = forms.ChoiceField(
                label=_("Expire inactive contacts"),
                help_text=_("When inactive contacts should be removed from the flow"),
                initial=5,
                choices=IVRCall.IVR_EXPIRES_CHOICES,
                widget=SelectWidget(attrs={"widget_only": False}),
            )
            keyword_triggers = forms.CharField(
                required=False,
                label=_("Global keyword triggers"),
                help_text=_("When a user sends any of these keywords they will begin this flow"),
                widget=SelectWidget(
                    attrs={
                        "widget_only": False,
                        "multi": True,
                        "searchable": True,
                        "tags": True,
                        "space_select": True,
                        "placeholder": _("Keywords"),
                    }
                ),
            )

            def __init__(self, user, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.user = user

                metadata = self.instance.metadata

                # IVR retries
                ivr_retry = self.fields["ivr_retry"]
                ivr_retry.initial = metadata.get("ivr_retry", self.fields["ivr_retry"].initial)

                flow_triggers = Trigger.objects.filter(
                    org=self.instance.org,
                    flow=self.instance,
                    is_archived=False,
                    groups=None,
                    trigger_type=Trigger.TYPE_KEYWORD,
                ).order_by("created_on")

                keyword_triggers = self.fields["keyword_triggers"]
                keyword_triggers.initial = ",".join(t.keyword for t in flow_triggers)

            class Meta:
                model = Flow
                fields = ("name", "keyword_triggers", "expires_after_minutes", "ignore_triggers", "ivr_retry")
                widgets = {"name": InputWidget(), "ignore_triggers": CheckboxWidget()}

        class FlowUpdateForm(BaseUpdateFlowFormMixin, BaseFlowForm):
            keyword_triggers = forms.CharField(
                required=False,
                label=_("Global keyword triggers"),
                help_text=_("When a user sends any of these keywords they will begin this flow"),
                widget=SelectWidget(
                    attrs={
                        "widget_only": False,
                        "multi": True,
                        "searchable": True,
                        "tags": True,
                        "space_select": True,
                        "placeholder": _("Keywords"),
                    }
                ),
            )

            expires_after_minutes = forms.ChoiceField(
                label=_("Expire inactive contacts"),
                help_text=_("When inactive contacts should be removed from the flow"),
                initial=str(60 * 24 * 7),
                choices=EXPIRES_CHOICES,
                widget=SelectWidget(attrs={"widget_only": False}),
            )

            def __init__(self, user, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.user = user

                flow_triggers = Trigger.objects.filter(
                    org=self.instance.org,
                    flow=self.instance,
                    is_archived=False,
                    groups=None,
                    trigger_type=Trigger.TYPE_KEYWORD,
                ).order_by("created_on")

                keyword_triggers = self.fields["keyword_triggers"]
                keyword_triggers.initial = list([t.keyword for t in flow_triggers])

            class Meta:
                model = Flow
                fields = ("name", "keyword_triggers", "expires_after_minutes", "ignore_triggers")
                widgets = {"name": InputWidget(), "ignore_triggers": CheckboxWidget()}

        success_message = ""
        success_url = "uuid@flows.flow_editor"

        def get_form_class(self):
            flow_type = self.object.flow_type

            if flow_type == Flow.TYPE_VOICE:
                return self.IVRFlowUpdateForm
            elif flow_type == Flow.TYPE_SURVEY:
                return self.SurveyFlowUpdateForm
            else:
                return self.FlowUpdateForm

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def pre_save(self, obj):
            obj = super().pre_save(obj)
            metadata = obj.metadata

            if Flow.CONTACT_CREATION in self.form.cleaned_data:
                metadata[Flow.CONTACT_CREATION] = self.form.cleaned_data[Flow.CONTACT_CREATION]

            if "ivr_retry" in self.form.cleaned_data:
                metadata[Flow.METADATA_IVR_RETRY] = int(self.form.cleaned_data["ivr_retry"])

            obj.metadata = metadata
            return obj

        def post_save(self, obj):
            keywords = set()
            user = self.request.user
            org = user.get_org()

            if "keyword_triggers" in self.form.cleaned_data:

                existing_keywords = set(
                    t.keyword
                    for t in obj.triggers.filter(
                        org=org, flow=obj, trigger_type=Trigger.TYPE_KEYWORD, is_archived=False, groups=None
                    )
                )

                if len(self.form.cleaned_data["keyword_triggers"]) > 0:
                    keywords = set(self.form.cleaned_data["keyword_triggers"].split(","))

                removed_keywords = existing_keywords.difference(keywords)
                for keyword in removed_keywords:
                    obj.triggers.filter(org=org, flow=obj, keyword=keyword, groups=None, is_archived=False).update(
                        is_archived=True
                    )

                added_keywords = keywords.difference(existing_keywords)
                archived_keywords = [
                    t.keyword
                    for t in obj.triggers.filter(
                        org=org, flow=obj, trigger_type=Trigger.TYPE_KEYWORD, is_archived=True, groups=None
                    )
                ]

                # set difference does not have a deterministic order, we need to sort the keywords
                for keyword in sorted(added_keywords):
                    # first check if the added keyword is not amongst archived
                    if keyword in archived_keywords:  # pragma: needs cover
                        obj.triggers.filter(org=org, flow=obj, keyword=keyword, groups=None).update(is_archived=False)
                    else:
                        Trigger.objects.create(
                            org=org,
                            keyword=keyword,
                            trigger_type=Trigger.TYPE_KEYWORD,
                            flow=obj,
                            created_by=user,
                            modified_by=user,
                        )

            # run async task to update all runs
            from .tasks import update_run_expirations_task

            on_transaction_commit(lambda: update_run_expirations_task.delay(obj.pk))

            return obj

    class UploadActionRecording(OrgObjPermsMixin, SmartUpdateView):
        def post(self, request, *args, **kwargs):  # pragma: needs cover
            path = self.save_recording_upload(
                self.request.FILES["file"], self.request.POST.get("actionset"), self.request.POST.get("action")
            )
            return JsonResponse(dict(path=path))

        def save_recording_upload(self, file, actionset_id, action_uuid):  # pragma: needs cover
            flow = self.get_object()
            return public_file_storage.save(
                "recordings/%d/%d/steps/%s.wav" % (flow.org.pk, flow.id, action_uuid), file
            )

    class UploadMediaAction(OrgObjPermsMixin, SmartUpdateView):
        slug_url_kwarg = "uuid"

        def post(self, request, *args, **kwargs):
            return JsonResponse(self.save_media_upload(self.request.FILES["file"]))

        def save_media_upload(self, file):
            flow = self.get_object()
            name_uuid = str(uuid4())
            extension = file.name.split(".")[-1]

            # browsers might send m4a files but correct MIME type is audio/mp4
            if extension == "m4a":
                file.content_type = "audio/mp4"

            url = public_file_storage.save(
                "attachments/%d/%d/steps/%s.%s" % (flow.org.pk, flow.id, name_uuid, extension), file
            )
            return {"type": file.content_type, "url": f"{settings.STORAGE_URL}/{url}"}

    class BaseList(OrgQuerysetMixin, OrgPermsMixin, BulkActionMixin, SmartListView):
        title = _("Flows")
        refresh = 10000
        fields = ("name", "modified_on")
        default_template = "flows/flow_list.html"
        default_order = ("-saved_on",)
        search_fields = ("name__icontains",)

        def get_context_data(self, **kwargs):
            context = super().get_context_data(**kwargs)
            context["org_has_flows"] = Flow.objects.filter(org=self.request.user.get_org(), is_active=True).count()
            context["folders"] = self.get_folders()
            context["labels"] = self.get_flow_labels()
            context["campaigns"] = self.get_campaigns()
            context["request_url"] = self.request.path

            # decorate flow objects with their run activity stats
            for flow in context["object_list"]:
                flow.run_stats = flow.get_run_stats()

            return context

        def derive_queryset(self, *args, **kwargs):
            qs = super().derive_queryset(*args, **kwargs)
            return qs.exclude(is_system=True).exclude(is_active=False)

        def get_campaigns(self):
            from temba.campaigns.models import CampaignEvent

            org = self.request.user.get_org()
            events = CampaignEvent.objects.filter(
                campaign__org=org,
                is_active=True,
                campaign__is_active=True,
                flow__is_archived=False,
                flow__is_active=True,
                flow__is_system=False,
            )
            return (
                events.values("campaign__name", "campaign__id").annotate(count=Count("id")).order_by("campaign__name")
            )

        def apply_bulk_action(self, user, action, objects, label):
            super().apply_bulk_action(user, action, objects, label)

            if action == "archive":
                ignored = objects.filter(is_archived=False)
                if ignored:
                    flow_names = ", ".join([f.name for f in ignored])
                    raise forms.ValidationError(
                        _("The following flows are still used by campaigns so could not be archived: %(flows)s"),
                        params={"flows": flow_names},
                    )

        def get_bulk_action_labels(self):
            return self.get_user().get_org().flow_labels.all()

        def get_flow_labels(self):
            labels = []
            for label in FlowLabel.objects.filter(org=self.request.user.get_org(), parent=None):
                labels.append(
                    dict(pk=label.pk, label=label.name, count=label.get_flows_count(), children=label.children.all())
                )
            return labels

        def get_folders(self):
            org = self.request.user.get_org()

            return [
                dict(
                    label="Active",
                    url=reverse("flows.flow_list"),
                    count=Flow.objects.exclude(is_system=True)
                    .filter(is_active=True, is_archived=False, org=org)
                    .count(),
                ),
                dict(
                    label="Archived",
                    url=reverse("flows.flow_archived"),
                    count=Flow.objects.exclude(is_system=True)
                    .filter(is_active=True, is_archived=True, org=org)
                    .count(),
                ),
            ]

    class Archived(BaseList):
        bulk_actions = ("restore",)
        default_order = ("-created_on",)

        def derive_queryset(self, *args, **kwargs):
            return super().derive_queryset(*args, **kwargs).filter(is_active=True, is_archived=True)

    class List(BaseList):
        title = _("Flows")
        bulk_actions = ("archive", "label")

        def derive_queryset(self, *args, **kwargs):
            queryset = super().derive_queryset(*args, **kwargs)
            queryset = queryset.filter(is_active=True, is_archived=False)
            return queryset

    class Campaign(BaseList, OrgObjPermsMixin):
        bulk_actions = ("label",)
        campaign = None

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r"^%s/%s/(?P<campaign_id>\d+)/$" % (path, action)

        def derive_title(self, *args, **kwargs):
            return self.get_campaign().name

        def get_object_org(self):
            from temba.campaigns.models import Campaign

            return Campaign.objects.get(pk=self.kwargs["campaign_id"]).org

        def get_campaign(self):
            if not self.campaign:
                from temba.campaigns.models import Campaign

                campaign_id = self.kwargs["campaign_id"]
                self.campaign = Campaign.objects.filter(id=campaign_id, org=self.request.user.get_org()).first()
            return self.campaign

        def get_queryset(self, **kwargs):
            from temba.campaigns.models import CampaignEvent

            flow_ids = CampaignEvent.objects.filter(
                campaign=self.get_campaign(), flow__is_archived=False, flow__is_system=False
            ).values("flow__id")

            flows = Flow.objects.filter(id__in=flow_ids, org=self.request.user.get_org()).order_by("-modified_on")
            return flows

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)
            context["current_campaign"] = self.get_campaign()
            return context

    class Filter(BaseList, OrgObjPermsMixin):
        add_button = True
        bulk_actions = ("label",)

        def get_gear_links(self):
            links = []

            label = FlowLabel.objects.get(pk=self.kwargs["label_id"])

            if self.has_org_perm("flows.flow_update"):
                # links.append(dict(title=_("Edit"), href="#", js_class="label-update-btn"))

                links.append(
                    dict(
                        id="update-label",
                        title=_("Edit"),
                        style="button-primary",
                        href=f"{reverse('flows.flowlabel_update', args=[label.pk])}",
                        modax=_("Edit Label"),
                    )
                )

            if self.has_org_perm("flows.flow_delete"):
                links.append(
                    dict(
                        id="delete-label",
                        title=_("Delete Label"),
                        href=f"{reverse('flows.flowlabel_delete', args=[label.pk])}",
                        modax=_("Delete Label"),
                    )
                )

            return links

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)
            context["current_label"] = self.derive_label()
            return context

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r"^%s/%s/(?P<label_id>\d+)/$" % (path, action)

        def derive_title(self, *args, **kwargs):
            return self.derive_label().name

        def get_object_org(self):
            return FlowLabel.objects.get(pk=self.kwargs["label_id"]).org

        def derive_label(self):
            return FlowLabel.objects.get(pk=self.kwargs["label_id"], org=self.request.user.get_org())

        def get_label_filter(self):
            label = FlowLabel.objects.get(pk=self.kwargs["label_id"])
            children = label.children.all()
            if children:  # pragma: needs cover
                return [l for l in FlowLabel.objects.filter(parent=label)] + [label]
            else:
                return [label]

        def get_queryset(self, **kwargs):
            qs = super().get_queryset(**kwargs)
            qs = qs.filter(org=self.request.user.get_org()).order_by("-created_on")
            qs = qs.filter(labels__in=self.get_label_filter(), is_archived=False).distinct()

            return qs

    class EditorNext(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        slug_url_kwarg = "uuid"

        def get(self, request, *args, **kwargs):
            # redirect to the editor endpoint
            return HttpResponseRedirect(reverse("flows.flow_editor", args=[self.get_object().uuid]))

    class Editor(OrgObjPermsMixin, SmartReadView):
        slug_url_kwarg = "uuid"

        def derive_title(self):
            return self.object.name

        def get_template_names(self):
            return "flows/flow_editor.haml"

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)

            dev_mode = getattr(settings, "EDITOR_DEV_MODE", False)
            getattr(settings, "EDITOR_DEV_MODE", False)
            prefix = "/dev" if dev_mode else settings.STATIC_URL

            # get our list of assets to incude
            scripts = []
            styles = []

            if dev_mode:  # pragma: no cover
                response = requests.get("http://localhost:3000/asset-manifest.json")
                data = response.json()
            else:
                with open("node_modules/@nyaruka/flow-editor/build/asset-manifest.json") as json_file:
                    data = json.load(json_file)

            for key, filename in data.get("files").items():

                # tack on our prefix for dev mode
                filename = prefix + filename

                # ignore precache manifest
                if key.startswith("precache-manifest") or key.startswith("service-worker"):
                    continue

                # css files
                if key.endswith(".css") and filename.endswith(".css"):
                    styles.append(filename)

                # javascript
                if key.endswith(".js") and filename.endswith(".js"):
                    scripts.append(filename)

            flow = self.object

            context["scripts"] = scripts
            context["styles"] = styles
            context["migrate"] = "migrate" in self.request.GET

            if flow.is_archived:
                context["mutable"] = False
                context["can_start"] = False
                context["can_simulate"] = False
            else:
                context["mutable"] = self.has_org_perm("flows.flow_update") and not self.request.user.is_superuser
                context["can_start"] = flow.flow_type != Flow.TYPE_VOICE or flow.org.supports_ivr()
                context["can_simulate"] = True

            context["dev_mode"] = dev_mode
            context["is_starting"] = flow.is_starting()

            feature_filters = []

            facebook_channel = flow.org.get_channel_for_role(Channel.ROLE_SEND, scheme=URN.FACEBOOK_SCHEME)
            if facebook_channel is not None:
                feature_filters.append("facebook")

            whatsapp_channel = flow.org.get_channel_for_role(Channel.ROLE_SEND, scheme=URN.WHATSAPP_SCHEME)
            if whatsapp_channel is not None:
                feature_filters.append("whatsapp")

            if flow.org.is_connected_to_dtone():
                feature_filters.append("airtime")

            if flow.org.classifiers.filter(is_active=True).exists():
                feature_filters.append("classifier")

            ticketers = flow.org.ticketers.filter(is_active=True)
            if not self.request.user.is_beta():
                ticketers = ticketers.exclude(ticketer_type="internal")

            if ticketers.exists():
                feature_filters.append("ticketer")

            if flow.org.get_resthooks():
                feature_filters.append("resthook")

            context["feature_filters"] = json.dumps(feature_filters)

            return context

        def get_gear_links(self):
            links = []
            flow = self.object
            if (
                flow.flow_type != Flow.TYPE_SURVEY
                and self.has_org_perm("flows.flow_broadcast")
                and not flow.is_archived
            ):
                links.append(
                    dict(
                        id="start-flow",
                        title=_("Start Flow"),
                        style="button-primary",
                        href=f"{reverse('flows.flow_broadcast', args=[self.object.pk])}",
                        modax=_("Start Flow"),
                    )
                )

            if self.has_org_perm("flows.flow_results"):
                links.append(
                    dict(
                        title=_("Results"),
                        style="button-primary",
                        href=reverse("flows.flow_results", args=[flow.uuid]),
                    )
                )
            if len(links) > 1:
                links.append(dict(divider=True))

            if self.has_org_perm("flows.flow_update") and not flow.is_archived:
                links.append(
                    dict(
                        id="edit-flow",
                        title=_("Edit"),
                        href=f"{reverse('flows.flow_update', args=[self.object.pk])}",
                        modax=_("Edit Flow"),
                    )
                )

            if self.has_org_perm("flows.flow_copy"):
                links.append(dict(title=_("Copy"), posterize=True, href=reverse("flows.flow_copy", args=[flow.id])))

            if self.has_org_perm("flows.flow_delete"):
                links.append(
                    dict(
                        id="delete-flow",
                        title=_("Delete"),
                        href=f"{reverse('flows.flow_delete', args=[self.object.pk])}",
                        modax=_("Delete Flow"),
                    )
                )

            links.append(dict(divider=True)),

            if self.has_org_perm("orgs.org_export"):
                links.append(dict(title=_("Export Definition"), href=f"{reverse('orgs.org_export')}?flow={flow.id}"))
            if self.has_org_perm("flows.flow_export_translation"):
                links.append(
                    dict(
                        id="export-translation",
                        title=_("Export Translation"),
                        href=f"{reverse('flows.flow_export_translation', args=[self.object.pk])}",
                        modax=_("Export Translation"),
                    )
                )

            if self.has_org_perm("flows.flow_import_translation"):
                links.append(
                    dict(title=_("Import Translation"), href=reverse("flows.flow_import_translation", args=[flow.id]))
                )

            user = self.get_user()
            if user.is_superuser or user.is_staff:
                links.append(
                    dict(
                        title=_("Service"),
                        posterize=True,
                        href=f'{reverse("orgs.org_service")}?organization={flow.org_id}&redirect_url={reverse("flows.flow_editor", args=[flow.uuid])}',
                    )
                )

            return links

    class ChangeLanguage(OrgObjPermsMixin, SmartUpdateView):
        class Form(forms.Form):
            language = forms.CharField(required=True)

            def __init__(self, user, instance, *args, **kwargs):
                self.user = user

                super().__init__(*args, **kwargs)

            def clean_language(self):
                data = self.cleaned_data["language"]
                if data and data not in self.user.get_org().get_language_codes():
                    raise ValidationError(_("Not a valid language."))

                return data

        form_class = Form
        success_url = "uuid@flows.flow_editor"

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def form_valid(self, form):
            flow_def = mailroom.get_client().flow_change_language(
                self.object.get_definition(), form.cleaned_data["language"]
            )

            self.object.save_revision(self.get_user(), flow_def)

            return HttpResponseRedirect(self.get_success_url())

    class ExportTranslation(OrgObjPermsMixin, ModalMixin, SmartUpdateView):
        class Form(forms.Form):
            language = forms.ChoiceField(
                required=False,
                label=_("Language"),
                help_text=_("Include translations in this language."),
                choices=[("", "None")],
                widget=SelectWidget(),
            )
            include_args = forms.BooleanField(
                required=False,
                label=_("Include Arguments"),
                initial=True,
                help_text=_("Include arguments to tests on splits"),
                widget=CheckboxWidget(),
            )

            def __init__(self, user, instance, *args, **kwargs):
                super().__init__(*args, **kwargs)

                org = user.get_org()
                org_languages = org.languages.all().order_by("orgs", "name")

                self.user = user
                self.fields["language"].choices += [(lang.iso_code, lang.name) for lang in org_languages]

        form_class = Form
        submit_button_name = _("Export")
        success_url = "@flows.flow_list"

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def form_valid(self, form):
            params = {
                "flow": self.object.id,
                "language": form.cleaned_data["language"],
                "exclude_args": "0" if form.cleaned_data["include_args"] else "1",
            }
            download_url = reverse("flows.flow_download_translation") + "?" + urlencode(params, doseq=True)

            # if this is an XHR request, we need to return a structured response that it can parse
            if "HTTP_X_PJAX" in self.request.META:
                response = self.render_to_response(
                    self.get_context_data(
                        form=form,
                        success_url=self.get_success_url(),
                        success_script=getattr(self, "success_script", None),
                    )
                )
                response["Temba-Success"] = download_url
                return response

            return HttpResponseRedirect(download_url)

    class DownloadTranslation(OrgObjPermsMixin, SmartListView):
        """
        Download link for PO translation files extracted from flows by mailroom
        """

        def get_object_org(self):
            self.flows = Flow.objects.filter(id__in=self.request.GET.getlist("flow"), is_active=True)
            flow_orgs = {flow.org for flow in self.flows}
            return self.flows[0].org if len(flow_orgs) == 1 else None

        def get(self, request, *args, **kwargs):
            org = self.request.user.get_org()

            language = request.GET.get("language", "")
            exclude_args = request.GET.get("exclude_args") == "1"

            filename = slugify_with(self.flows[0].name) if len(self.flows) == 1 else "flows"
            if language:
                filename += f".{language}"
            filename += ".po"

            po = Flow.export_translation(org, self.flows, language, exclude_args)

            response = HttpResponse(po, content_type="text/x-gettext-translation")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

    class ImportTranslation(OrgObjPermsMixin, SmartUpdateView):
        class UploadForm(forms.Form):
            po_file = forms.FileField(label=_("PO translation file"), required=True)

            def __init__(self, user, instance, *args, **kwargs):
                super().__init__(*args, **kwargs)

                self.flow = instance

            def clean_po_file(self):
                data = self.cleaned_data["po_file"]
                if data:
                    try:
                        po_info = gettext.po_get_info(data.read().decode())
                    except Exception:
                        raise ValidationError(_("File doesn't appear to be a valid PO file."))

                    if po_info.language_code:
                        if po_info.language_code == self.flow.base_language:
                            raise ValidationError(
                                _("Contains translations in %(lang)s which is the base language of this flow."),
                                params={"lang": po_info.language_name},
                            )

                        if not self.flow.org.languages.filter(iso_code=po_info.language_code).exists():
                            raise ValidationError(
                                _("Contains translations in %(lang)s which is not a supported translation language."),
                                params={"lang": po_info.language_name},
                            )

                return data

        class ConfirmForm(forms.Form):
            language = forms.ChoiceField(
                label=_("Language"),
                help_text=_("Replace flow translations in this language."),
                required=True,
                widget=SelectWidget(),
            )

            def __init__(self, user, instance, *args, **kwargs):
                super().__init__(*args, **kwargs)

                org = user.get_org()
                languages = org.languages.exclude(iso_code=instance.base_language).order_by("name")

                self.fields["language"].choices += [(lang.iso_code, lang.name) for lang in languages]

        title = _("Import Translation")
        submit_button_name = _("Import")
        success_url = "uuid@flows.flow_editor"

        def get_form_class(self):
            return self.ConfirmForm if self.request.GET.get("po") else self.UploadForm

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def form_valid(self, form):
            org = self.request.user.get_org()
            po_uuid = self.request.GET.get("po")

            if not po_uuid:
                po_file = form.cleaned_data["po_file"]
                po_uuid = gettext.po_save(org, po_file)

                return HttpResponseRedirect(
                    reverse("flows.flow_import_translation", args=[self.object.id]) + f"?po={po_uuid}"
                )
            else:
                po_data = gettext.po_load(org, po_uuid)
                language = form.cleaned_data["language"]

                updated_defs = Flow.import_translation(self.object.org, [self.object], language, po_data)
                self.object.save_revision(self.request.user, updated_defs[str(self.object.uuid)])

                analytics.track(self.request.user.username, "temba.flow_po_imported")

            return HttpResponseRedirect(self.get_success_url())

        @cached_property
        def po_info(self):
            po_uuid = self.request.GET.get("po")
            if not po_uuid:
                return None

            org = self.request.user.get_org()
            po_data = gettext.po_load(org, po_uuid)
            return gettext.po_get_info(po_data)

        def get_context_data(self, *args, **kwargs):
            org = self.request.user.get_org()

            context = super().get_context_data(*args, **kwargs)
            context["show_upload_form"] = not self.po_info
            context["po_info"] = self.po_info
            context["flow_language"] = org.languages.filter(iso_code=self.object.base_language).first()
            return context

        def derive_initial(self):
            return {"language": self.po_info.language_code if self.po_info else ""}

    class ExportResults(ModalMixin, OrgPermsMixin, SmartFormView):
        class ExportForm(forms.Form):
            flows = forms.ModelMultipleChoiceField(
                Flow.objects.filter(id__lt=0), required=True, widget=forms.MultipleHiddenInput()
            )

            group_memberships = forms.ModelMultipleChoiceField(
                queryset=ContactGroup.user_groups.none(),
                required=False,
                label=_("Groups"),
                widget=SelectMultipleWidget(attrs={"placeholder": _("Optional: Group memberships")}),
            )

            contact_fields = forms.ModelMultipleChoiceField(
                ContactField.user_fields.filter(id__lt=0),
                required=False,
                label=("Fields"),
                widget=SelectMultipleWidget(
                    attrs={"placeholder": _("Optional: Fields to include"), "searchable": True}
                ),
            )

            extra_urns = forms.MultipleChoiceField(
                required=False,
                label=_("URNs"),
                choices=URN.SCHEME_CHOICES,
                widget=SelectMultipleWidget(
                    attrs={"placeholder": _("Optional: URNs in addition to the one used in the flow")}
                ),
            )

            responded_only = forms.BooleanField(
                required=False,
                label=_("Responded Only"),
                initial=True,
                help_text=_("Only export results for contacts which responded"),
                widget=CheckboxWidget(),
            )
            include_msgs = forms.BooleanField(
                required=False,
                label=_("Include Messages"),
                help_text=_("Export all messages sent and received in this flow"),
                widget=CheckboxWidget(),
            )

            def __init__(self, user, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.user = user
                self.fields[ExportFlowResultsTask.CONTACT_FIELDS].queryset = ContactField.user_fields.active_for_org(
                    org=self.user.get_org()
                ).order_by(Lower("label"))

                self.fields[ExportFlowResultsTask.GROUP_MEMBERSHIPS].queryset = ContactGroup.user_groups.filter(
                    org=self.user.get_org(), is_active=True, status=ContactGroup.STATUS_READY
                ).order_by(Lower("name"))

                self.fields[ExportFlowResultsTask.FLOWS].queryset = Flow.objects.filter(
                    org=self.user.get_org(), is_active=True
                )

            def clean(self):
                cleaned_data = super().clean()

                if (
                    ExportFlowResultsTask.CONTACT_FIELDS in cleaned_data
                    and len(cleaned_data[ExportFlowResultsTask.CONTACT_FIELDS])
                    > ExportFlowResultsTask.MAX_CONTACT_FIELDS_COLS
                ):  # pragma: needs cover
                    raise forms.ValidationError(
                        _(
                            f"You can only include up to {ExportFlowResultsTask.MAX_CONTACT_FIELDS_COLS} contact fields in your export"
                        )
                    )

                if (
                    ExportFlowResultsTask.GROUP_MEMBERSHIPS in cleaned_data
                    and len(cleaned_data[ExportFlowResultsTask.GROUP_MEMBERSHIPS])
                    > ExportFlowResultsTask.MAX_GROUP_MEMBERSHIPS_COLS
                ):  # pragma: needs cover
                    raise forms.ValidationError(
                        _(
                            f"You can only include up to {ExportFlowResultsTask.MAX_GROUP_MEMBERSHIPS_COLS} groups for group memberships in your export"
                        )
                    )

                return cleaned_data

        form_class = ExportForm
        submit_button_name = _("Download Results")
        success_url = "@flows.flow_list"

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            return kwargs

        def derive_initial(self):
            flow_ids = self.request.GET.get("ids", None)
            if flow_ids:  # pragma: needs cover
                return dict(
                    flows=Flow.objects.filter(
                        org=self.request.user.get_org(), is_active=True, id__in=flow_ids.split(",")
                    )
                )
            else:
                return dict()

        def form_valid(self, form):
            analytics.track(self.request.user.username, "temba.flow_exported")

            user = self.request.user
            org = user.get_org()

            # is there already an export taking place?
            existing = ExportFlowResultsTask.get_recent_unfinished(org)
            if existing:
                messages.info(
                    self.request,
                    _(
                        "There is already an export in progress, started by %s. You must wait "
                        "for that export to complete before starting another." % existing.created_by.username
                    ),
                )
            else:
                export = ExportFlowResultsTask.create(
                    org,
                    user,
                    form.cleaned_data[ExportFlowResultsTask.FLOWS],
                    contact_fields=form.cleaned_data[ExportFlowResultsTask.CONTACT_FIELDS],
                    include_msgs=form.cleaned_data[ExportFlowResultsTask.INCLUDE_MSGS],
                    responded_only=form.cleaned_data[ExportFlowResultsTask.RESPONDED_ONLY],
                    extra_urns=form.cleaned_data[ExportFlowResultsTask.EXTRA_URNS],
                    group_memberships=form.cleaned_data[ExportFlowResultsTask.GROUP_MEMBERSHIPS],
                )
                on_transaction_commit(lambda: export_flow_results_task.delay(export.pk))

                if not getattr(settings, "CELERY_ALWAYS_EAGER", False):  # pragma: needs cover
                    messages.info(
                        self.request,
                        _("We are preparing your export. We will e-mail you at %s when it is ready.")
                        % self.request.user.username,
                    )

                else:
                    export = ExportFlowResultsTask.objects.get(id=export.pk)
                    dl_url = reverse("assets.download", kwargs=dict(type="results_export", pk=export.pk))
                    messages.info(
                        self.request,
                        _("Export complete, you can find it here: %s (production users will get an email)") % dl_url,
                    )

            if "HTTP_X_PJAX" not in self.request.META:
                return HttpResponseRedirect(self.get_success_url())
            else:  # pragma: no cover
                response = self.render_to_response(
                    self.get_context_data(
                        form=form,
                        success_url=self.get_success_url(),
                        success_script=getattr(self, "success_script", None),
                    )
                )
                response["Temba-Success"] = self.get_success_url()
                response["REDIRECT"] = self.get_success_url()
                return response

    class ActivityChart(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        """
        Intercooler helper that renders a chart of activity by a given period
        """

        # the min number of responses to show a histogram
        HISTOGRAM_MIN = 0

        # the min number of responses to show the period charts
        PERIOD_MIN = 0

        EXIT_TYPES = {
            None: "active",
            FlowRun.EXIT_TYPE_COMPLETED: "completed",
            FlowRun.EXIT_TYPE_INTERRUPTED: "interrupted",
            FlowRun.EXIT_TYPE_EXPIRED: "expired",
        }

        def get_context_data(self, *args, **kwargs):

            total_responses = 0
            context = super().get_context_data(*args, **kwargs)

            flow = self.get_object()
            from temba.flows.models import FlowPathCount

            from_uuids = flow.metadata["waiting_exit_uuids"]
            dates = FlowPathCount.objects.filter(flow=flow, from_uuid__in=from_uuids).aggregate(
                Max("period"), Min("period")
            )
            start_date = dates.get("period__min")
            end_date = dates.get("period__max")

            # by hour of the day
            hod = FlowPathCount.objects.filter(flow=flow, from_uuid__in=from_uuids).extra(
                {"hour": "extract(hour from period::timestamp)"}
            )
            hod = hod.values("hour").annotate(count=Sum("count")).order_by("hour")
            hod_dict = {int(h.get("hour")): h.get("count") for h in hod}

            hours = []
            for x in range(0, 24):
                hours.append({"bucket": datetime(1970, 1, 1, hour=x), "count": hod_dict.get(x, 0)})

            # by day of the week
            dow = FlowPathCount.objects.filter(flow=flow, from_uuid__in=from_uuids).extra(
                {"day": "extract(dow from period::timestamp)"}
            )
            dow = dow.values("day").annotate(count=Sum("count"))
            dow_dict = {int(d.get("day")): d.get("count") for d in dow}

            dow = []
            for x in range(0, 7):
                day_count = dow_dict.get(x, 0)
                dow.append({"day": x, "count": day_count})
                total_responses += day_count

            if total_responses > self.PERIOD_MIN:
                dow = sorted(dow, key=lambda k: k["day"])
                days = (
                    _("Sunday"),
                    _("Monday"),
                    _("Tuesday"),
                    _("Wednesday"),
                    _("Thursday"),
                    _("Friday"),
                    _("Saturday"),
                )
                dow = [
                    {
                        "day": days[d["day"]],
                        "count": d["count"],
                        "pct": 100 * float(d["count"]) / float(total_responses),
                    }
                    for d in dow
                ]
                context["dow"] = dow
                context["hod"] = hours

            if total_responses > self.HISTOGRAM_MIN:
                # our main histogram
                date_range = end_date - start_date
                histogram = FlowPathCount.objects.filter(flow=flow, from_uuid__in=from_uuids)
                if date_range < timedelta(days=21):
                    histogram = histogram.extra({"bucket": "date_trunc('hour', period)"})
                    min_date = start_date - timedelta(hours=1)
                elif date_range < timedelta(days=500):
                    histogram = histogram.extra({"bucket": "date_trunc('day', period)"})
                    min_date = end_date - timedelta(days=100)
                else:
                    histogram = histogram.extra({"bucket": "date_trunc('week', period)"})
                    min_date = end_date - timedelta(days=500)

                histogram = histogram.values("bucket").annotate(count=Sum("count")).order_by("bucket")
                context["histogram"] = histogram

                # highcharts works in UTC, but we want to offset our chart according to the org timezone
                context["min_date"] = min_date

            counts = FlowRunCount.objects.filter(flow=flow).values("exit_type").annotate(Sum("count"))

            total_runs = 0
            for count in counts:
                key = self.EXIT_TYPES[count["exit_type"]]
                context[key] = count["count__sum"]
                total_runs += count["count__sum"]

            # make sure we have a value for each one
            for state in ("expired", "interrupted", "completed", "active"):
                if state not in context:
                    context[state] = 0

            context["total_runs"] = total_runs
            context["total_responses"] = total_responses

            return context

    class RunTable(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        """
        Intercooler helper which renders rows of runs to be embedded in an existing table with infinite scrolling
        """

        paginate_by = 50

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)
            flow = self.get_object()
            runs = flow.runs.all()

            if str_to_bool(self.request.GET.get("responded", "true")):
                runs = runs.filter(responded=True)

            # paginate
            modified_on = self.request.GET.get("modified_on", None)
            if modified_on:
                id = self.request.GET["id"]

                modified_on = iso8601.parse_date(modified_on)
                runs = runs.filter(modified_on__lte=modified_on).exclude(id=id)

            # we grab one more than our page to denote whether there's more to get
            runs = list(runs.order_by("-modified_on")[: self.paginate_by + 1])
            context["more"] = len(runs) > self.paginate_by
            runs = runs[: self.paginate_by]

            result_fields = flow.metadata["results"]

            # populate result values
            for run in runs:
                results = run.results
                run.value_list = []
                for result_field in result_fields:
                    run.value_list.append(results.get(result_field["key"], None))

            context["runs"] = runs
            context["start_date"] = flow.org.get_delete_date(archive_type=Archive.TYPE_FLOWRUN)
            context["paginate_by"] = self.paginate_by
            return context

    class CategoryCounts(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        slug_url_kwarg = "uuid"

        def render_to_response(self, context, **response_kwargs):
            return JsonResponse(self.get_object().get_category_counts())

    class Results(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        slug_url_kwarg = "uuid"

        def get_gear_links(self):
            links = []

            if self.has_org_perm("flows.flow_update"):
                links.append(
                    dict(
                        id="download-results",
                        title=_("Download"),
                        modax=_("Download Flow Results"),
                        href=f"{reverse('flows.flow_export_results')}?ids={self.get_object().pk}",
                    )
                )

            if self.has_org_perm("flows.flow_editor"):
                links.append(
                    dict(
                        title=_("Edit Flow"),
                        style="button-primary",
                        href=reverse("flows.flow_editor", args=[self.get_object().uuid]),
                    )
                )

            return links

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)
            flow = self.get_object()

            result_fields = []
            for result_field in flow.metadata[Flow.METADATA_RESULTS]:
                if not result_field["name"].startswith("_"):
                    result_field = result_field.copy()
                    result_field["has_categories"] = "true" if len(result_field["categories"]) > 1 else "false"
                    result_fields.append(result_field)
            context["result_fields"] = result_fields

            context["categories"] = flow.get_category_counts()["counts"]
            context["utcoffset"] = int(datetime.now(flow.org.timezone).utcoffset().total_seconds() // 60)
            return context

    class Activity(AllowOnlyActiveFlowMixin, OrgObjPermsMixin, SmartReadView):
        slug_url_kwarg = "uuid"

        def get(self, request, *args, **kwargs):
            flow = self.get_object(self.get_queryset())
            (active, visited) = flow.get_activity()

            return JsonResponse(dict(nodes=active, segments=visited, is_starting=flow.is_starting()))

    class Simulate(OrgObjPermsMixin, SmartReadView):
        @csrf_exempt
        def dispatch(self, *args, **kwargs):
            return super().dispatch(*args, **kwargs)

        def get(self, request, *args, **kwargs):  # pragma: needs cover
            return HttpResponseRedirect(reverse("flows.flow_editor", args=[self.get_object().uuid]))

        def post(self, request, *args, **kwargs):
            try:
                json_dict = json.loads(request.body)
            except Exception as e:  # pragma: needs cover
                return JsonResponse(dict(status="error", description="Error parsing JSON: %s" % str(e)), status=400)

            if not settings.MAILROOM_URL:  # pragma: no cover
                return JsonResponse(
                    dict(status="error", description="mailroom not configured, cannot simulate"), status=500
                )

            analytics.track(request.user.username, "temba.flow_simulated")

            flow = self.get_object()
            client = mailroom.get_client()

            channel_uuid = "440099cf-200c-4d45-a8e7-4a564f4a0e8b"
            channel_name = "Test Channel"

            # build our request body, which includes any assets that mailroom should fake
            payload = {
                "org_id": flow.org_id,
                "assets": {
                    "channels": [
                        {
                            "uuid": channel_uuid,
                            "name": channel_name,
                            "address": "+18005551212",
                            "schemes": ["tel"],
                            "roles": ["send", "receive", "call"],
                            "country": "US",
                        }
                    ]
                },
            }

            if "flow" in json_dict:
                payload["flows"] = [{"uuid": flow.uuid, "definition": json_dict["flow"]}]

            # check if we are triggering a new session
            if "trigger" in json_dict:
                payload["trigger"] = json_dict["trigger"]

                # ivr flows need a connection in their trigger
                if flow.flow_type == Flow.TYPE_VOICE:
                    payload["trigger"]["connection"] = {
                        "channel": {"uuid": channel_uuid, "name": channel_name},
                        "urn": "tel:+12065551212",
                    }

                payload["trigger"]["environment"] = flow.org.as_environment_def()

                try:
                    return JsonResponse(client.sim_start(payload))
                except mailroom.MailroomException:
                    return JsonResponse(dict(status="error", description="mailroom error"), status=500)

            # otherwise we are resuming
            elif "resume" in json_dict:
                payload["resume"] = json_dict["resume"]
                payload["resume"]["environment"] = flow.org.as_environment_def()
                payload["session"] = json_dict["session"]

                try:
                    return JsonResponse(client.sim_resume(payload))
                except mailroom.MailroomException:
                    return JsonResponse(dict(status="error", description="mailroom error"), status=500)

    class Broadcast(ModalMixin, OrgObjPermsMixin, SmartUpdateView):
        class BroadcastForm(forms.ModelForm):
            def __init__(self, *args, **kwargs):
                self.user = kwargs.pop("user")
                self.flow = kwargs.pop("flow")
                super().__init__(*args, **kwargs)

            omnibox = JSONField(
                label=_("Contacts & Groups"),
                required=False,
                help_text=_("These contacts will be added to the flow, sending the first message if appropriate."),
                widget=OmniboxChoice(
                    attrs={
                        "placeholder": _("Recipients, enter contacts or groups"),
                        "groups": True,
                        "contacts": True,
                        "widget_only": True,
                    }
                ),
            )

            restart_participants = forms.BooleanField(
                label=_("Restart Participants"),
                required=False,
                initial=False,
                help_text=_("Restart any contacts already participating in this flow"),
                widget=CheckboxWidget(),
            )

            include_active = forms.BooleanField(
                label=_("Include Active Contacts"),
                required=False,
                initial=False,
                help_text=_("Include contacts currently active in a flow"),
                widget=CheckboxWidget(),
            )

            recipients_mode = forms.ChoiceField(
                widget=SelectWidget(
                    attrs={"placeholder": _("Select contacts or groups to start in the flow"), "widget_only": True}
                ),
                choices=(
                    ("select", _("Enter contacts and groups to start below")),
                    ("query", _("Search for contacts to start")),
                ),
                initial="select",
            )

            contact_query = forms.CharField(
                required=False,
                widget=ContactSearchWidget(attrs={"widget_only": True, "placeholder": _("Enter contact query")}),
            )

            def clean_contact_query(self):
                contact_query = self.cleaned_data["contact_query"]
                recipients_mode = self.data["recipients_mode"]

                if recipients_mode == "query":
                    if not contact_query.strip():
                        raise ValidationError(_("Contact query is required"))

                    try:
                        parsed = parse_query(self.flow.org, contact_query)
                        contact_query = parsed.query
                    except SearchException as e:
                        raise ValidationError(str(e))

                return contact_query

            def clean_omnibox(self):
                starting = self.cleaned_data["omnibox"]
                recipients_mode = self.data["recipients_mode"]

                if recipients_mode == "select" and not starting:  # pragma: needs cover
                    raise ValidationError(_("You must specify at least one contact or one group to start a flow."))

                return omnibox_deserialize(self.user.get_org(), starting)

            def clean(self):

                cleaned = super().clean()

                # check whether there are any flow starts that are incomplete
                if self.flow.is_starting():
                    raise ValidationError(
                        _(
                            "This flow is already being started, please wait until that process is complete before "
                            "starting more contacts."
                        )
                    )

                if self.flow.org.is_suspended:
                    raise ValidationError(
                        _(
                            "Sorry, your workspace is currently suspended. "
                            "To enable starting flows, please contact support."
                        )
                    )
                if self.flow.org.is_flagged:
                    raise ValidationError(
                        _(
                            "Sorry, your workspace is currently flagged. To enable starting flows, please contact support."
                        )
                    )

                return cleaned

            class Meta:
                model = Flow
                fields = ("omnibox", "restart_participants", "include_active")

        form_class = BroadcastForm
        fields = ("omnibox", "restart_participants", "include_active", "recipients_mode", "contact_query")
        success_message = ""
        submit_button_name = _("Start Flow")
        success_url = "uuid@flows.flow_editor"

        def has_facebook_topic(self, flow):
            if not flow.is_legacy():
                definition = flow.get_current_revision().get_migrated_definition()
                for node in definition.get("nodes", []):
                    for action in node.get("actions", []):
                        if action.get("type", "") == "send_msg" and action.get("topic", ""):
                            return True

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)
            flow = self.get_object()
            org = flow.org

            warnings = []

            # facebook channels need to warn if no topic is set
            facebook_channel = org.get_channel_for_role(Channel.ROLE_SEND, scheme=URN.FACEBOOK_SCHEME)
            if facebook_channel:
                if not self.has_facebook_topic(flow):
                    warnings.append(
                        _(
                            "This flow does not specify a Facebook topic. You may still start this flow but Facebook contacts who have not sent an incoming message in the last 24 hours may not receive it."
                        )
                    )

            # if we have a whatsapp channel
            whatsapp_channel = org.get_channel_for_role(Channel.ROLE_SEND, scheme=URN.WHATSAPP_SCHEME)
            if whatsapp_channel:
                # check to see we are using templates
                templates = flow.get_dependencies_metadata("template")
                if not templates:
                    warnings.append(
                        _(
                            "This flow does not use message templates. You may still start this flow but WhatsApp contacts who have not sent an incoming message in the last 24 hours may not receive it."
                        )
                    )

                # check that this template is synced and ready to go
                for ref in templates:
                    template = Template.objects.filter(org=org, uuid=ref["uuid"]).first()
                    if not template:
                        warnings.append(
                            _(f"The message template {ref['name']} does not exist on your account and cannot be sent.")
                        )
                    elif not template.is_approved():
                        warnings.append(
                            _(f"Your message template {template.name} is not approved and cannot be sent.")
                        )

            run_stats = self.object.get_run_stats()

            context["warnings"] = warnings
            context["run_count"] = run_stats["total"]
            context["complete_count"] = run_stats["completed"]
            return context

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["user"] = self.request.user
            kwargs["flow"] = self.object
            return kwargs

        def save(self, *args, **kwargs):
            form = self.form
            flow = self.object

            recipients_mode = form.cleaned_data["recipients_mode"]

            # save off our broadcast info
            groups = []
            contacts = []
            contact_query = None

            if recipients_mode == "query":
                contact_query = form.cleaned_data["contact_query"]
            else:
                omnibox = form.cleaned_data["omnibox"]
                groups = list(omnibox["groups"])
                contacts = list(omnibox["contacts"])

            analytics.track(
                self.request.user.username,
                "temba.flow_broadcast",
                dict(contacts=len(contacts), groups=len(groups), query=contact_query),
            )

            # activate all our contacts
            flow.async_start(
                self.request.user,
                groups,
                contacts,
                contact_query,
                restart_participants=form.cleaned_data["restart_participants"],
                include_active=form.cleaned_data["include_active"],
            )
            return flow

    class Assets(OrgPermsMixin, SmartTemplateView):
        """
        Provides environment and languages to the new editor
        """

        @classmethod
        def derive_url_pattern(cls, path, action):
            return rf"^{path}/{action}/(?P<org>\d+)/(?P<fingerprint>[\w-]+)/(?P<type>environment|language)/((?P<uuid>[a-z0-9-]{{36}})/)?$"

        def derive_org(self):
            if not hasattr(self, "org"):
                self.org = Org.objects.get(id=self.kwargs["org"])
            return self.org

        def get(self, *args, **kwargs):
            org = self.derive_org()
            asset_type_name = kwargs["type"]

            if asset_type_name == "environment":
                return JsonResponse(org.as_environment_def())
            else:
                languages = org.languages.filter(is_active=True).order_by("id")
                return JsonResponse({"results": [{"iso": l.iso_code, "name": l.name} for l in languages]})


# this is just for adhoc testing of the preprocess url
class PreprocessTest(FormView):  # pragma: no cover
    @csrf_exempt
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def post(self, request, *args, **kwargs):
        return HttpResponse(
            json.dumps(dict(text="Norbert", extra=dict(occupation="hoopster", skillz=7.9))),
            content_type="application/json",
        )


class FlowLabelForm(forms.ModelForm):
    name = forms.CharField(required=True, widget=InputWidget())
    parent = forms.ModelChoiceField(
        FlowLabel.objects.all(),
        required=False,
        label=_("Parent"),
        widget=SelectWidget(attrs={"widget_only": True, "placeholder": _("Optional: Select parent label")}),
    )
    flows = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        self.org = kwargs["org"]
        del kwargs["org"]

        label = None
        if "label" in kwargs:
            label = kwargs["label"]
            del kwargs["label"]

        super().__init__(*args, **kwargs)
        qs = FlowLabel.objects.filter(org=self.org, parent=None)

        if label:
            qs = qs.exclude(id=label.pk)

        self.fields["parent"].queryset = qs

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if FlowLabel.objects.filter(org=self.org, name=name).exclude(pk=self.instance.id).exists():
            raise ValidationError(_("Name already used"))
        return name

    class Meta:
        model = FlowLabel
        fields = "__all__"


class FlowLabelCRUDL(SmartCRUDL):
    model = FlowLabel
    actions = ("create", "update", "delete")

    class Delete(OrgObjPermsMixin, SmartDeleteView):
        success_url = "@flows.flow_list"
        redirect_url = "@flows.flow_list"
        cancel_url = "@flows.flow_list"
        success_message = ""
        submit_button_name = _("Delete")

    class Update(ModalMixin, OrgObjPermsMixin, SmartUpdateView):
        form_class = FlowLabelForm
        success_url = "id@flows.flow_filter"
        success_message = ""

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["org"] = self.request.user.get_org()
            kwargs["label"] = self.get_object()
            return kwargs

        def derive_fields(self):
            if FlowLabel.objects.filter(parent=self.get_object()):  # pragma: needs cover
                return ("name",)
            else:
                return ("name", "parent")

    class Create(ModalMixin, OrgPermsMixin, SmartCreateView):
        fields = ("name", "parent", "flows")
        success_url = "@flows.flow_list"
        form_class = FlowLabelForm
        success_message = ""
        submit_button_name = _("Create")

        def get_form_kwargs(self):
            kwargs = super().get_form_kwargs()
            kwargs["org"] = self.request.user.get_org()
            return kwargs

        def pre_save(self, obj, *args, **kwargs):
            obj = super().pre_save(obj, *args, **kwargs)
            obj.org = self.request.user.get_org()
            return obj

        def post_save(self, obj, *args, **kwargs):
            obj = super().post_save(obj, *args, **kwargs)

            flow_ids = []
            if self.form.cleaned_data["flows"]:  # pragma: needs cover
                flow_ids = [int(f) for f in self.form.cleaned_data["flows"].split(",") if f.isdigit()]

            flows = Flow.objects.filter(org=obj.org, is_active=True, pk__in=flow_ids)

            if flows:  # pragma: needs cover
                obj.toggle_label(flows, add=True)

            return obj


class FlowStartCRUDL(SmartCRUDL):
    model = FlowStart
    actions = ("list",)

    class List(OrgQuerysetMixin, OrgPermsMixin, SmartListView):
        title = _("Flow Start Log")
        ordering = ("-created_on",)
        select_related = ("flow", "created_by")
        paginate_by = 25

        def get_gear_links(self):
            return [dict(title=_("Flows"), style="button-light", href=reverse("flows.flow_list"),)]

        def derive_queryset(self, *args, **kwargs):
            return (
                super()
                .derive_queryset(*args, **kwargs)
                .exclude(created_by=None)
                .prefetch_related("contacts", "groups")
            )

        def get_context_data(self, *args, **kwargs):
            context = super().get_context_data(*args, **kwargs)

            FlowStartCount.bulk_annotate(context["object_list"])

            return context
