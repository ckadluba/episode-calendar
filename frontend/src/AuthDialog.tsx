import { useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  clearSession,
  deleteAccount,
  logout,
  startEmailLogin,
  verifyEmailCode,
  type AuthSession,
} from "./auth";

type AuthDialogProps = {
  open: boolean;
  session: AuthSession | null;
  notice: string | null;
  onClose: () => void;
  onSignedIn: (session: AuthSession) => void;
  onSignedOut: () => void;
};

type Step = "email" | "code";

function errorMessage(error: unknown, fallback: string) {
  if (!(error instanceof ApiError)) return fallback;
  if (error.status === 400) return "Der Code ist ungültig oder abgelaufen.";
  if (error.status === 422) return "Bitte gib eine gültige E-Mail-Adresse ein.";
  if (error.status === 429) return "Zu viele Versuche. Bitte warte einen Moment.";
  if (error.status === 502) return "Die Anmeldemail konnte nicht gesendet werden.";
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
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  useEffect(() => {
    if (!open) return;
    setStep("email");
    setCode("");
    setBusy(false);
    setError(null);
    setInfo(null);
    setConfirmingDelete(false);
    if (session) setEmail(session.user.email);
  }, [open, session]);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [open, onClose]);

  if (!open) return null;

  const submitEmail = async (event: FormEvent) => {
    event.preventDefault();
    if (!email.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await startEmailLogin(email.trim());
      setStep("code");
      setInfo("Wenn die Adresse gültig ist, ist die E-Mail unterwegs.");
    } catch (cause) {
      setError(errorMessage(cause, "Die Anmeldemail konnte nicht gesendet werden."));
    } finally {
      setBusy(false);
    }
  };

  const submitCode = async (event: FormEvent) => {
    event.preventDefault();
    if (code.length !== 6 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await verifyEmailCode(email.trim(), code);
      onSignedIn(next);
    } catch (cause) {
      setError(errorMessage(cause, "Die Anmeldung ist fehlgeschlagen."));
    } finally {
      setBusy(false);
    }
  };

  const resend = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await startEmailLogin(email.trim());
      setCode("");
      setInfo("Ein neuer Code wurde verschickt. Der alte Code ist nicht mehr gültig.");
    } catch (cause) {
      setError(errorMessage(cause, "Die Anmeldemail konnte nicht gesendet werden."));
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
              Angemeldet als <strong>{session.user.email}</strong>. Deine Filter, Vorab-Einstellungen
              und gesehenen Folgen werden jetzt zentral gespeichert und sind auf allen Geräten
              verfügbar.
            </p>
            {error && <p className="auth-error">{error}</p>}
            {confirmingDelete ? (
              <>
                <p className="auth-warning">
                  Dabei werden Konto und gespeicherte Einstellungen endgültig gelöscht. Die lokalen
                  Einstellungen in diesem Browser bleiben erhalten.
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
              <div className="auth-actions">
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
              Anmelden ohne Passwort. Du bekommst einen sechsstelligen Code und einen Link per
              E-Mail. Der Code funktioniert auf jedem Gerät.
            </p>

            {step === "email" ? (
              <form onSubmit={submitEmail} className="auth-form">
                <label className="auth-field">
                  E-Mail-Adresse
                  <input
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    placeholder="du@example.com"
                    autoComplete="email"
                    autoFocus
                    required
                  />
                </label>
                <button className="primary-button" type="submit" disabled={busy}>
                  {busy ? "Wird gesendet …" : "Code senden"}
                </button>
                <div className="auth-links">
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy || !email.trim()}
                    onClick={() => {
                      setStep("code");
                      setCode("");
                      setError(null);
                      setInfo("Gib den Code ein, den du auf einem anderen Gerät erhalten hast.");
                    }}
                  >
                    Ich habe bereits einen Code
                  </button>
                </div>
              </form>
            ) : (
              <form onSubmit={submitCode} className="auth-form">
                <p className="auth-hint">
                  Code an <strong>{email}</strong>
                </p>
                <label className="auth-field">
                  Anmeldecode
                  <input
                    className="auth-code"
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    maxLength={6}
                    value={code}
                    onChange={(event) =>
                      setCode(event.target.value.replace(/\D/g, "").slice(0, 6))
                    }
                    placeholder="123456"
                    autoComplete="one-time-code"
                    autoFocus
                    required
                  />
                </label>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={busy || code.length !== 6}
                >
                  {busy ? "Wird geprüft …" : "Anmelden"}
                </button>
                <div className="auth-links">
                  <button type="button" className="link-button" disabled={busy} onClick={resend}>
                    Neuen Code senden
                  </button>
                  <button
                    type="button"
                    className="link-button"
                    disabled={busy}
                    onClick={() => {
                      setStep("email");
                      setCode("");
                      setError(null);
                      setInfo(null);
                    }}
                  >
                    Andere E-Mail verwenden
                  </button>
                </div>
              </form>
            )}

            {info && <p className="auth-info">{info}</p>}
            {error && <p className="auth-error">{error}</p>}
            {notice && <p className="auth-error">{notice}</p>}
            <p className="auth-hint">
              Du kannst den Link in der E-Mail auch auf einem anderen Gerät öffnen. Ohne Anmeldung
              bleiben deine Einstellungen nur in diesem Browser.
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
