"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
  deleteLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsDelete,
  getLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsGet,
  saveLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsPost,
} from "@/client/sdk.gen";
import type { LangfuseCredentialsResponse } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/lib/auth";

export function TelemetrySection() {
  const { user, loading: authLoading } = useAuth();
  const [credentials, setCredentials] = useState<LangfuseCredentialsResponse>({
    host: "",
    public_key: "",
    secret_key: "",
    project_id: "",
    traces_public: false,
    configured: false,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const hasFetched = useRef(false);

  useEffect(() => {
    if (authLoading || !user || hasFetched.current) {
      return;
    }
    hasFetched.current = true;
    fetchCredentials();
  }, [authLoading, user]);

  async function fetchCredentials() {
    try {
      const { data } = await getLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsGet();
      if (data) {
        setCredentials(data);
      }
    } catch {
      // No credentials configured yet — that's fine
    } finally {
      setLoading(false);
    }
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const { data, error } = await saveLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsPost({
        body: {
          host: credentials.host ?? "",
          public_key: credentials.public_key ?? "",
          secret_key: credentials.secret_key ?? "",
          project_id: credentials.project_id ?? "",
          traces_public: credentials.traces_public ?? false,
        },
      });
      if (error) {
        throw new Error("Failed to save");
      }
      // The API looks the project ID up when left blank; say so when it couldn't.
      const saved = data as { project_id?: string } | undefined;
      if (saved && saved.project_id === "") {
        toast.warning(
          "Saved, but the Langfuse project ID could not be detected. Enter it so trace links open.",
        );
      } else {
        toast.success("Telemetry credentials saved");
      }
      await fetchCredentials();
    } catch {
      toast.error("Failed to save telemetry credentials");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setSaving(true);
    try {
      await deleteLangfuseCredentialsApiV1OrganizationsLangfuseCredentialsDelete();
      setCredentials({
        host: "",
        public_key: "",
        secret_key: "",
        project_id: "",
        traces_public: false,
        configured: false,
      });
      toast.success("Telemetry credentials removed");
    } catch {
      toast.error("Failed to remove telemetry credentials");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading...</p>;
  }

  return (
    <form onSubmit={handleSave} className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Connect your Langfuse project to receive call tracing data.
      </p>
      <div className="space-y-2">
        <Label htmlFor="langfuse-host">Host</Label>
        <Input
          id="langfuse-host"
          placeholder="https://cloud.langfuse.com"
          value={credentials.host}
          onChange={(e) => setCredentials({ ...credentials, host: e.target.value })}
          required
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="langfuse-public-key">Public Key</Label>
        <Input
          id="langfuse-public-key"
          placeholder="pk-lf-..."
          value={credentials.public_key}
          onChange={(e) => setCredentials({ ...credentials, public_key: e.target.value })}
          required
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="langfuse-secret-key">Secret Key</Label>
        <Input
          id="langfuse-secret-key"
          type="password"
          placeholder="sk-lf-..."
          value={credentials.secret_key}
          onChange={(e) => setCredentials({ ...credentials, secret_key: e.target.value })}
          required
        />
      </div>
      <div className="space-y-2">
        <Label htmlFor="langfuse-project-id">Project ID</Label>
        <Input
          id="langfuse-project-id"
          placeholder="Detected automatically from the keys"
          value={credentials.project_id ?? ""}
          onChange={(e) => setCredentials({ ...credentials, project_id: e.target.value })}
        />
        <p className="text-xs text-muted-foreground">
          Used for trace links. Leave blank to detect it, or copy it from your Langfuse URL:
          /project/&lt;project-id&gt;/...
        </p>
      </div>
      <div className="flex items-start justify-between gap-4 rounded-md border p-3">
        <div className="space-y-1">
          <Label htmlFor="langfuse-traces-public">Public trace links</Label>
          <p className="text-xs text-muted-foreground">
            Off: only people signed in to your Langfuse project can open a trace. On: anyone
            with a trace link can read the whole call, including the transcript.
          </p>
        </div>
        <Switch
          id="langfuse-traces-public"
          checked={credentials.traces_public ?? false}
          onCheckedChange={(checked) => setCredentials({ ...credentials, traces_public: checked })}
        />
      </div>
      <div className="flex gap-2">
        <Button type="submit" disabled={saving}>
          {saving ? "Saving..." : "Save"}
        </Button>
        {credentials.configured && (
          <Button type="button" variant="destructive" disabled={saving} onClick={handleDelete}>
            Remove
          </Button>
        )}
      </div>
    </form>
  );
}
