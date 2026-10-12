import type {IconName} from '@/shared/ui/icon';

export type SettingsSection = 'appearance' | 'model';

export const SETTINGS_SECTIONS: {
  section: SettingsSection;
  icon: IconName;
  label: string;
}[] = [
  {section: 'appearance', icon: 'palette', label: 'Appearance'},
  {section: 'model', icon: 'neurology', label: 'Model'},
];
