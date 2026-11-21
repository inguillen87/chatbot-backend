"""Read-only Meta authority for a credential already opened by the renewal job."""
from services import meta_credential_operations as ops
from services.meta_whatsapp_cloud import MetaSenderSnapshot
from services.meta_whatsapp_runtime import MetaOutboundRuntime, MetaRuntimeError, _object_body


REJECTION_REASONS = frozenset({
    "meta_token_authority_invalid",
    "meta_token_authority_expired",
    "meta_token_granular_scope_mismatch",
    "meta_phone_membership_unverified",
    "meta_app_subscription_unverified",
})


def revalidate_credential(target, credential, config, *, graph_get, clock, require_default_callback=False):
    """Return a current proof and the provider expiry, or a fixed, redacted error."""
    def get(method, url, **kwargs):
        # The runtime constructs only its three authority GETs. Its native TLS
        # adapter disables redirects and HTTP debugging, including debug_token.
        if method != "GET":
            raise MetaRuntimeError("meta_authority_temporarily_unavailable", transient=True)
        response = graph_get(method, url, **kwargs)
        status = getattr(response, "status_code", None)
        if type(status) is int and 400 <= status < 500:
            # debug_token authenticates with the *app* credential. An error
            # on that request does not establish revocation of the target token.
            if status in (400, 401, 403) and not url.endswith("/debug_token"):
                error = _object_body(response).get("error")
                if (isinstance(error, dict) and error.get("is_transient") is not True
                        and error.get("code") in (102, 190)):
                    raise MetaRuntimeError("meta_token_authority_invalid")
            raise MetaRuntimeError("meta_authority_temporarily_unavailable", transient=True)
        return response

    try:
        runtime = MetaOutboundRuntime(ops._authority_config(target, config), get=get, clock=clock)
        sender = MetaSenderSnapshot(target.tenant_id, target.connection_id, target.sender_id,
            target.app_id, target.waba_id, target.phone_id, target.environment)
        return runtime.observe_authority(sender, credential, for_renewal=True,
                                         require_default_callback=require_default_callback)
    except MetaRuntimeError as exc:
        if not exc.transient and exc.code in REJECTION_REASONS:
            raise MetaRuntimeError(exc.code) from None
    except ops.MetaCredentialOperationError:
        pass
    raise MetaRuntimeError("meta_authority_temporarily_unavailable", transient=True)
