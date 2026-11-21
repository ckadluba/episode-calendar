import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  addPasskey,
  authenticationOptions,
  clearSession,
  completeAuthentication,
  completeRegistration,
  deleteAccount,
  deletePasskey,
  listPasskeys,
  logout,
  passkeyAdditionOptions,
  recoverAccount,
  registrationOptions,
  rotateRecoveryCode,
  type AuthSession,
  type AuthSessionWithRecovery,
  type PasskeyInfo,
} from "./auth";
import { creationOptions, requestOptions, serializeCredential, webauthnSupported } from "./webauthn";

type AuthDialogProps = {
  open: boolean;
  session: AuthSession | null;
  notice: string | null;
  onClose: () => void;
  onSignedIn: (session: AuthSession, keepOpen?: boolean) => void;
  onSignedOut: () => void;
};

type Step = "start" | "recovery";

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    if (error.status === 400) return "Die Anmeldung ist fehlgeschlagen. Bitte versuche es erneut.";
    if (error.status === 401) return "Die Sitzung ist abgelaufen. Bitte melde dich erneut an.";
    if (error.status === 404) return "Der Passkey wurde nicht gefunden.";
  }
  if (error instanceof Error && error.name === "NotAllowedError") {
    return "Die Passkey-Abfrage wurde abgebrochen.";
  }
  return fallback;
}

export function AuthDialog({
  open,
  session,
  notice,
  onClose,
  onSignedIn,
  onSignedOut,
}: AuthDialogProps) {
  const [step, setStep] = useState<Step>("start");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [oneTimeCode, setOneTimeCode] = useState<string | null>(null);
  const [passkeys, setPasskeys] = useState<PasskeyInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const loadPasskeys = useCallback(async (current: AuthSession) => {
    try {
      setPasskeys(await listPasskeys(current));
    } catch {
      setPasskeys([]);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setStep("start");
    setRecoveryCode("");
    setOneTimeCode(null);
    setBusy(false);
    setError(null);
    setInfo(null);
    setConfirmingDelete(false);
    if (session) void loadPasskeys(session);
  }, [open]); // Reset only when the dialog opens; signing in must not clear the code.

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [open, onClose]);

  if (!open) return null;

  const signedIn = async (next: AuthSessionWithRecovery) => {
    setOneTimeCode(next.recovery_code);
    // Keep the dialog open so the one-time recovery code can be copied.
    onSignedIn(next, true);
  };

  const signInWithPasskey = async () => {
    if (!webauthnSupported()) {
      setError("Dieser Browser unterstützt keine Passkeys.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const options = await authenticationOptions();
      const credential = await navigator.credentials.get({ publicKey: requestOptions(options) });
      if (!credential) throw new Error("cancelled");
      const next = await completeAuthentication(
        options.challenge,
        serializeCredential(credential as PublicKeyCredential),
      );
      onSignedIn(next);
    } catch (cause) {
      setError(errorMessage(cause, "Die Anmeldung mit Passkey ist fehlgeschlagen."));
    } finally {
      setBusy(false);
    }
  };

  const createPasskey = async () => {
    if (!webauthnSupported()) {
      setError("Dieser Browser unterstützt keine Passkeys.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const options = await registrationOptions();
      const credential = await navigator.credentials.create({ publicKey: creationOptions(options) });
      if (!credential) throw new Error("cancelled");
      await signedIn(
        await completeRegistration(
          options.challenge,
          serializeCredential(credential as PublicKeyCredential),
        ),
      );
    } catch (cause) {
      setError(errorMessage(cause, "Der Passkey konnte nicht erstellt werden."));
    } finally {
      setBusy(false);
    }
  };

  const submitRecovery = async (event: FormEvent) => {
    event.preventDefault();
    if (!recoveryCode.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await signedIn(await recoverAccount(recoveryCode.trim()));
      setRecoveryCode("");
      setStep("start");
    } catch (cause) {
      setError(errorMessage(cause, "Der Recovery-Code ist ungültig."));
    } finally {
      setBusy(false);
    }
  };

  const addAnotherPasskey = async () => {
    if (!session || !webauthnSupported()) return;
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      const options = await passkeyAdditionOptions(session);
      const credential = await navigator.credentials.create({ publicKey: creationOptions(options) });
      if (!credential) throw new Error("cancelled");
      await addPasskey(session, options.challenge, serializeCredential(credential as PublicKeyCredential));
      await loadPasskeys(session);
      setInfo("Der neue Passkey ist gespeichert.");
    } catch (cause) {
      setError(errorMessage(cause, "Der Passkey konnte nicht hinzugefügt werden."));
    } finally {
      setBusy(false);
    }
  };

  const removePasskey = async (passkey: PasskeyInfo) => {
    if (!session || busy) return;
    setBusy(true);
    setError(null);
    try {
      await deletePasskey(session, passkey.credential_id);
      await loadPasskeys(session);
    } catch (cause) {
      setError(errorMessage(cause, "Der Passkey konnte nicht gelöscht werden."));
    } finally {
      setBusy(false);
    }
  };

  const regenerateRecoveryCode = async () => {
    if (!session || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await rotateRecoveryCode(session);
      setOneTimeCode(result.recovery_code);
    } catch (cause) {
      setError(errorMessage(cause, "Der Recovery-Code konnte nicht erneuert werden."));
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    if (!session || busy) return;
    setBusy(true);
    try {
      await logout(session);
    } catch {
      // The local session is cleared even if the server is unreachable.
    } finally {
      clearSession();
      setBusy(false);
      onSignedOut();
    }
  };

  const removeAccount = async () => {
    if (!session || busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteAccount(session);
      clearSession();
      onSignedOut();
    } catch (cause) {
      setError(errorMessage(cause, "Das Konto konnte nicht gelöscht werden."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="confirm-backdrop" onClick={onClose}>
      <div
        className="confirm-dialog auth-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="auth-title">{session ? "Dein Konto" : "Anmelden"}</h2>

        {session ? (
          <>
            <p className="auth-intro">
              Angemeldet mit Passkey. Deine Filter, Vorab-Einstellungen und gesehenen Folgen werden
              synchronisiert. Wir speichern keinen Namen, keine E-Mail-Adresse und kein Passwort.
            </p>
            <p className="auth-hint">Konto: {session.user.id.slice(0, 8)}</p>

            <h3 className="auth-subtitle">Deine Passkeys</h3>
            {passkeys.length === 0 ? (
              <p className="auth-hint">Noch kein Passkey gespeichert.</p>
            ) : (
              <ul className="passkey-list">
                {passkeys.map((passkey) => (
                  <li key={passkey.credential_id}>
                    <span title={passkey.credential_id}>
                      {passkey.credential_id.slice(0, 12)}…
                      <small>
                        {new Date(passkey.created_at).toLocaleDateString("de-AT")}
                        {passkey.last_used_at
                          ? ` · zuletzt ${new Date(passkey.last_used_at).toLocaleDateString("de-AT")}`
                          : ""}
                      </small>
                    </span>
                    <button
                      type="button"
                      className="link-danger"
                      disabled={busy}
                      onClick={() => void removePasskey(passkey)}
                    >
                      Entfernen
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {oneTimeCode && <RecoveryCodeNotice code={oneTimeCode} />}
            {error && <p className="auth-error">{error}</p>}
            {info && <p className="auth-info">{info}</p>}

            {confirmingDelete ? (
              <>
                <p className="auth-warning">
                  Dabei werden Konto, Passkeys, Recovery-Code und gespeicherte Einstellungen
                  endgültig gelöscht. Die lokalen Einstellungen in diesem Browser bleiben erhalten.
                </p>
                <div className="confirm-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={busy}
                    onClick={() => setConfirmingDelete(false)}
                  >
                    Abbrechen
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    disabled={busy}
                    onClick={removeAccount}
                  >
                    Endgültig löschen
                  </button>
                </div>
              </>
            ) : (
              <div className="auth-actions auth-actions-stack">
                <button className="primary-button" type="button" disabled={busy} onClick={addAnotherPasskey}>
                  Passkey hinzufügen
                </button>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={busy}
                  onClick={regenerateRecoveryCode}
                >
                  Neuen Recovery-Code erzeugen
                </button>
                <button className="secondary-button" type="button" disabled={busy} onClick={signOut}>
                  Abmelden
                </button>
                <button
                  className="link-danger"
                  type="button"
                  disabled={busy}
                  onClick={() => setConfirmingDelete(true)}
                >
                  Konto löschen
                </button>
              </div>
            )}
          </>
        ) : (
          <>
            <p className="auth-intro">
              Anmelden ohne Passwort und ohne E-Mail-Adresse. Dein Passkey bleibt auf deinen Geräten;
              der Server speichert nur einen öffentlichen Schlüssel.
            </p>

            {step === "start" ? (
              <div className="auth-form">
                <button className="primary-button" type="button" disabled={busy} onClick={signInWithPasskey}>
                  {busy ? "Bitte warten …" : "Mit Passkey anmelden"}
                </button>
                <button className="secondary-button" type="button" disabled={busy} onClick={createPasskey}>
                  Neuen Passkey erstellen
                </button>
                <div className="auth-links">
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy}
                    onClick={() => {
                      setStep("recovery");
                      setError(null);
                      setInfo(null);
                    }}
                  >
                    Recovery-Code eingeben
                  </button>
                </div>
              </div>
            ) : (
              <form onSubmit={submitRecovery} className="auth-form">
                <label className="auth-field">
                  Recovery-Code
                  <input
                    type="text"
                    value={recoveryCode}
                    onChange={(event) => setRecoveryCode(event.target.value)}
                    placeholder="Code von einem anderen Gerät"
                    autoComplete="off"
                    autoFocus
                    required
                  />
                </label>
                <button className="primary-button" type="submit" disabled={busy || !recoveryCode.trim()}>
                  {busy ? "Wird geprüft …" : "Anmelden"}
                </button>
                <div className="auth-links">
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy}
                    onClick={() => {
                      setStep("start");
                      setError(null);
                      setInfo(null);
                    }}
                  >
                    Zurück
                  </button>
                </div>
              </form>
            )}

            {oneTimeCode && <RecoveryCodeNotice code={oneTimeCode} />}
            {error && <p className="auth-error">{error}</p>}
            {info && <p className="auth-info">{info}</p>}
            {notice && <p className="auth-error">{notice}</p>}
            <p className="auth-hint">
              Mit einem Passkey pro Gerät meldest du dich überall an. Geht ein Gerät verloren,
              hilft der Recovery-Code. Ohne Anmeldung bleiben deine Einstellungen nur in diesem
              Browser.
            </p>
          </>
        )}

        <div className="confirm-actions auth-close">
          <button className="secondary-button" type="button" onClick={onClose}>
            Schließen
          </button>
        </div>
      </div>
    </div>
  );
}

function RecoveryCodeNotice({ code }: { code: string }) {
  return (
    <div className="auth-info recovery-notice">
      <strong>Neuer Recovery-Code – nur jetzt sichtbar:</strong>
      <code>{code}</code>
      <span>Bewahre ihn sicher auf. Er wird bei der nächsten Anmeldung ersetzt.</span>
    </div>
  );
}
