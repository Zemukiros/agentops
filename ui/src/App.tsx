import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Outlet, RouterProvider, createBrowserRouter } from "react-router";
import { ApiError } from "./api/client";
import { session, useSession } from "./api/session";
import { Layout, type NavItem } from "./components/Layout";
import { NewTask } from "./pages/NewTask";
import { SignIn } from "./pages/SignIn";
import { TaskDetailPage } from "./pages/TaskDetail";
import { TaskHistory } from "./pages/TaskHistory";

function onAuthError(err: unknown) {
  // the active token stopped working (revoked / DB reset): drop it, back to sign-in
  if (err instanceof ApiError && err.isUnauthorized) {
    const a = session.active();
    if (a) session.remove(a.id);
  }
}

export const queryClient = new QueryClient({
  queryCache: new QueryCache({ onError: onAuthError }),
  mutationCache: new MutationCache({ onError: onAuthError }),
  defaultOptions: {
    queries: {
      retry: (n, err) => !(err instanceof ApiError && err.status < 500) && n < 2,
      refetchOnWindowFocus: false,
      staleTime: 2_000,
    },
  },
});

const NAV: NavItem[] = [{ to: "/tasks", label: "Tasks", icon: "list" }];

function RequireIdentity() {
  const { active } = useSession();
  if (!active) return <Navigate to="/sign-in" replace />;
  // remount pages on identity switch: different user, different data
  return <Outlet key={active.id} />;
}

const router = createBrowserRouter([
  { path: "/sign-in", element: <SignIn /> },
  {
    element: <RequireIdentity />,
    children: [
      {
        element: <Layout items={NAV} />,
        children: [
          { index: true, element: <Navigate to="/tasks" replace /> },
          { path: "/tasks", element: <TaskHistory /> },
          { path: "/tasks/new", element: <NewTask /> },
          { path: "/tasks/:id", element: <TaskDetailPage /> },
          { path: "*", element: <Navigate to="/tasks" replace /> },
        ],
      },
    ],
  },
]);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
