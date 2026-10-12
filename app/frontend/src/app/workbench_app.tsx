import {lazy, type ReactElement, Suspense} from 'react';
import {Navigate, Route, Routes} from 'react-router-dom';
import {ErrorBoundary} from '@/shared/ui/error_boundary';
import {NoIndex} from './not_found_page';
import {NotFoundPage} from './not_found_page';
import {ChatHistoryProvider} from '@/shared/hooks/history_context';
import {RunHistoryProvider} from '@/shared/hooks/history_context';
import {SystemStatusProvider} from '@/shared/hooks/system_status_context';
import {LaunchStatusProvider} from '@/shared/hooks/launch_status_context';
import {Layout} from './layout';
import {ChatWorkspace} from '@/features/chat/chat_workspace';
import {ExampleChat} from '@/features/chat/example_chat';
import {ThemeProvider} from '@/shared/hooks/theme_context';
import {DocumentSkeleton} from '@/shared/ui';

const RunDetail = lazy(() =>
  import('@/features/report/run_detail').then(module => ({
    default: module.RunDetail,
  })),
);

const LaunchOperations = lazy(() =>
  import('@/features/diagnostics/launch_operations').then(module => ({
    default: module.LaunchOperations,
  })),
);

const SpendView = lazy(() =>
  import('@/features/diagnostics/spend_view').then(module => ({
    default: module.SpendView,
  })),
);

function PageLoading() {
  return <DocumentSkeleton label="Loading page…" />;
}

function page(title: string, element: ReactElement) {
  return (
    <>
      <NoIndex title={title} />
      {element}
    </>
  );
}

function WorkbenchRoutes() {
  return (
    <Routes>
      <Route
        path="/operations"
        element={page('Launch operations', <LaunchOperations />)}
      />
      <Route
        path="/operations/spend"
        element={page('Model spend', <SpendView />)}
      />
      <Route path="/" element={page('Workspace', <ChatWorkspace />)} />
      <Route path="/chats/:id" element={page('Workspace', <ChatWorkspace />)} />
      <Route
        path="/examples/:id"
        element={page('Example chat', <ExampleChat />)}
      />
      <Route path="/runs" element={<Navigate to="/" replace />} />
      <Route path="/runs/new" element={<Navigate to="/" replace />} />
      {/* One required-param route makes tab changes reuse RunDetail instead of refetching and blanking the header. */}
      <Route path="/runs/:id" element={<Navigate to="details" replace />} />
      <Route
        path="/runs/:id/:tab"
        element={page('Goal Report', <RunDetail />)}
      />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}

export function WorkbenchApp() {
  // Keep the boundary outside theme/layout so their crashes are caught while
  // the shell persists across route changes.
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <SystemStatusProvider>
          <LaunchStatusProvider>
            <RunHistoryProvider>
              <ChatHistoryProvider>
                <Layout>
                  <Suspense fallback={<PageLoading />}>
                    <WorkbenchRoutes />
                  </Suspense>
                </Layout>
              </ChatHistoryProvider>
            </RunHistoryProvider>
          </LaunchStatusProvider>
        </SystemStatusProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}
