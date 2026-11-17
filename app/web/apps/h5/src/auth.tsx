import { createContext, useContext, type ReactNode } from "react";
import type { Me } from "@fdc/api-client";

type AuthContextValue = {
  me: Me;
  refreshMe: () => Promise<Me>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({
  me,
  refreshMe,
  children,
}: AuthContextValue & { children: ReactNode }) {
  return <AuthContext.Provider value={{ me, refreshMe }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
