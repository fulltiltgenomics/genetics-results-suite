// Keycloak "scripts" authenticator for the first-broker-login flow.
//
// Allows the brokered (Google/Apple) login to proceed only when the user's email matches the
// realm allow-list, BEFORE any Keycloak account is created. Non-allowlisted users get a clean
// Forbidden page instead of an account + a downstream oauth2-proxy 403. The allow-list mirrors
// oauth2-proxy and is read from realm attributes (set by deploy.sh / keycloak-bind-allowlist.sh):
//   allowedEmailDomains : comma/space separated domains, e.g. "broadinstitute.org"
//   allowedEmails       : comma/space separated full addresses (e.g. Apple privaterelay aliases)
//
// Packaged as a provider JAR (this file + META-INF/keycloak-scripts.json) and requires the
// Keycloak "scripts" feature. Provider id once deployed: "script-email-allowlist.js".

var AuthenticationFlowError = Java.type("org.keycloak.authentication.AuthenticationFlowError");
var SerializedBrokeredIdentityContext = Java.type("org.keycloak.authentication.authenticators.broker.util.SerializedBrokeredIdentityContext");
var AbstractIdpAuthenticator = Java.type("org.keycloak.authentication.authenticators.broker.AbstractIdpAuthenticator");
var Status = Java.type("jakarta.ws.rs.core.Response$Status");

function listOf(value) {
    if (!value) return [];
    return String(value).toLowerCase().split(/[,;\s]+/).filter(function (x) { return x.length > 0; });
}

function authenticate(context) {
    var realm = context.getRealm();
    var brokerCtx = SerializedBrokeredIdentityContext.readFromAuthenticationSession(
        context.getAuthenticationSession(), AbstractIdpAuthenticator.BROKERED_CONTEXT_NOTE);
    var email = (brokerCtx != null && brokerCtx.getEmail() != null)
        ? String(brokerCtx.getEmail()).toLowerCase().trim() : "";

    var domains = listOf(realm.getAttribute("allowedEmailDomains"));
    var emails = listOf(realm.getAttribute("allowedEmails"));

    var allowed = email.length > 0 && (
        emails.indexOf(email) >= 0 ||
        domains.some(function (d) { return email.endsWith("@" + d); })
    );

    if (allowed) {
        context.success();
        return;
    }

    LOG.warn("email-allowlist: denied broker login for '" + email + "'");
    var challenge = context.form()
        .setError("Access denied: " + (email || "your account") +
                  " is not authorized to use this application. Contact the administrator if you believe this is a mistake.")
        .createErrorPage(Status.FORBIDDEN);
    context.failure(AuthenticationFlowError.ACCESS_DENIED, challenge);
}
