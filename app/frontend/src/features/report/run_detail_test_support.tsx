import {Route, Routes, useLocation} from 'react-router-dom';
import {renderWithProviders} from '@/shared/testing/render';
import {RunDetail} from './run_detail';

function LocationDisplay() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

export function renderAt(path: string) {
  return renderWithProviders(
    <>
      <Routes>
        <Route path="/runs/:id" element={<RunDetail />} />
        <Route path="/runs/:id/:tab" element={<RunDetail />} />
      </Routes>
      <LocationDisplay />
    </>,
    {path},
  );
}
