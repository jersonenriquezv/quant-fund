"use client";

import { useState, useCallback } from "react";
import { usePolling } from "@/lib/hooks";
import { postApi } from "@/lib/api";
import type { ProfileResponse } from "@/lib/api";

export function ProfileSelector() {
  const { data } = usePolling<ProfileResponse>("/profile", 5000);
  const [switching, setSwitching] = useState(false);

  const handleChange = useCallback(
    async (e: React.ChangeEvent<HTMLSelectElement>) => {
      const profile = e.target.value;
      if (!data || profile === data.active) return;

      setSwitching(true);
      try {
        await postApi<ProfileResponse>("/profile", { profile });
      } catch (err) {
        console.error("Failed to switch profile:", err);
      } finally {
        setSwitching(false);
      }
    },
    [data]
  );

  if (!data) return null;

  const active = data.profiles[data.active];
  const isNonDefault = data.active !== "default";

  return (
    <div className="profile-selector">
      <div className="profile-control">
        <span
          className="profile-dot"
          style={{ background: active?.color || "#64748b" }}
        />
        <select
          value={data.active}
          onChange={handleChange}
          disabled={switching}
          className="profile-dropdown"
        >
          {Object.entries(data.profiles).map(([key, info]) => (
            <option key={key} value={key}>
              {info.label}
            </option>
          ))}
        </select>
      </div>

      {isNonDefault && (
        <div
          className="profile-warning"
          style={{ borderColor: active?.color || "#eab308" }}
        >
          {data.active.toUpperCase()} MODE
        </div>
      )}
    </div>
  );
}
