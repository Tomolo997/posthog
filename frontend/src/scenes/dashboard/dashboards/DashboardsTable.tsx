import { IconLock, IconPin, IconPinFilled, IconShare } from '@posthog/icons'
import { LemonInput } from '@posthog/lemon-ui'
import { useActions, useValues } from 'kea'
import { MemberSelect } from 'lib/components/MemberSelect'
import { ObjectTags } from 'lib/components/ObjectTags/ObjectTags'
import { DashboardPrivilegeLevel } from 'lib/constants'
import { IconCottage } from 'lib/lemon-ui/icons'
import { LemonButton } from 'lib/lemon-ui/LemonButton'
import { More } from 'lib/lemon-ui/LemonButton/More'
import { LemonDivider } from 'lib/lemon-ui/LemonDivider'
import { LemonMarkdown } from 'lib/lemon-ui/LemonMarkdown'
import { LemonRow } from 'lib/lemon-ui/LemonRow'
import { LemonTable, LemonTableColumn, LemonTableColumns } from 'lib/lemon-ui/LemonTable'
import { createdAtColumn, createdByColumn } from 'lib/lemon-ui/LemonTable/columnUtils'
import { Link } from 'lib/lemon-ui/Link'
import { Tooltip } from 'lib/lemon-ui/Tooltip'
import { DashboardEventSource } from 'lib/utils/eventUsageLogic'
import { dashboardLogic } from 'scenes/dashboard/dashboardLogic'
import { DashboardsFilters, dashboardsLogic } from 'scenes/dashboard/dashboards/dashboardsLogic'
import { deleteDashboardLogic } from 'scenes/dashboard/deleteDashboardLogic'
import { duplicateDashboardLogic } from 'scenes/dashboard/duplicateDashboardLogic'
import { teamLogic } from 'scenes/teamLogic'
import { urls } from 'scenes/urls'
import { userLogic } from 'scenes/userLogic'

import { dashboardsModel, nameCompareFunction } from '~/models/dashboardsModel'
import { AvailableFeature, DashboardBasicType, DashboardMode, DashboardType } from '~/types'

import { DASHBOARD_CANNOT_EDIT_MESSAGE } from '../DashboardHeader'

export function DashboardsTableContainer(): JSX.Element {
    const { dashboardsLoading } = useValues(dashboardsModel)
    const { dashboards, filters } = useValues(dashboardsLogic)

    return <DashboardsTable dashboards={dashboards} dashboardsLoading={dashboardsLoading} filters={filters} />
}

interface DashboardsTableProps {
    dashboards: DashboardBasicType[]
    filters: DashboardsFilters
    dashboardsLoading: boolean
    extraActions?: JSX.Element | JSX.Element[]
    hideActions?: boolean
}

export function DashboardsTable({
    dashboards,
    dashboardsLoading,
    filters,
    extraActions,
    hideActions,
}: DashboardsTableProps): JSX.Element {
    const { unpinDashboard, pinDashboard } = useActions(dashboardsModel)
    const { setFilters } = useActions(dashboardsLogic)
    const { hasAvailableFeature } = useValues(userLogic)
    const { currentTeam } = useValues(teamLogic)
    const { showDuplicateDashboardModal } = useActions(duplicateDashboardLogic)
    const { showDeleteDashboardModal } = useActions(deleteDashboardLogic)

    const columns: LemonTableColumns<DashboardType> = [
        {
            width: 0,
            dataIndex: 'pinned',
            render: function Render(pinned, { id }) {
                return (
                    <LemonButton
                        size="small"
                        onClick={
                            pinned
                                ? () => unpinDashboard(id, DashboardEventSource.DashboardsList)
                                : () => pinDashboard(id, DashboardEventSource.DashboardsList)
                        }
                        tooltip={pinned ? 'Unpin dashboard' : 'Pin dashboard'}
                        icon={pinned ? <IconPinFilled /> : <IconPin />}
                    />
                )
            },
        },
        {
            title: 'Name',
            dataIndex: 'name',
            width: '40%',
            render: function Render(name, { id, description, is_shared, effective_privilege_level }) {
                const isPrimary = id === currentTeam?.primary_dashboard
                const canEditDashboard = effective_privilege_level >= DashboardPrivilegeLevel.CanEdit
                return (
                    <div>
                        <div className="row-name">
                            <Link data-attr="dashboard-name" to={urls.dashboard(id)}>
                                {name || 'Untitled'}
                            </Link>
                            {is_shared && (
                                <Tooltip title="This dashboard is shared publicly.">
                                    <IconShare className="ml-1 text-base text-link" />
                                </Tooltip>
                            )}
                            {!canEditDashboard && (
                                <Tooltip title={DASHBOARD_CANNOT_EDIT_MESSAGE}>
                                    <IconLock className="ml-1 text-base text-muted" />
                                </Tooltip>
                            )}
                            {isPrimary && (
                                <Tooltip title="The primary dashboard is shown on the project home page.">
                                    <span>
                                        <IconCottage className="ml-1 text-base text-warning" />
                                    </span>
                                </Tooltip>
                            )}
                        </div>
                        {hasAvailableFeature(AvailableFeature.DASHBOARD_COLLABORATION) && description && (
                            <LemonMarkdown className="row-description max-w-100" lowKeyHeadings>
                                {description}
                            </LemonMarkdown>
                        )}
                    </div>
                )
            },
            sorter: nameCompareFunction,
        },
        ...(hasAvailableFeature(AvailableFeature.TAGGING)
            ? [
                  {
                      title: 'Tags',
                      dataIndex: 'tags' as keyof DashboardType,
                      render: function Render(tags: DashboardType['tags']) {
                          return tags ? <ObjectTags tags={tags} staticOnly /> : null
                      },
                  } as LemonTableColumn<DashboardType, keyof DashboardType | undefined>,
              ]
            : []),
        createdByColumn<DashboardType>() as LemonTableColumn<DashboardType, keyof DashboardType | undefined>,
        createdAtColumn<DashboardType>() as LemonTableColumn<DashboardType, keyof DashboardType | undefined>,
        hideActions
            ? {}
            : {
                  width: 0,
                  render: function RenderActions(_, { id, name }: DashboardType) {
                      return (
                          <More
                              overlay={
                                  <>
                                      <LemonButton
                                          to={urls.dashboard(id)}
                                          onClick={() => {
                                              dashboardLogic({ id }).mount()
                                              dashboardLogic({ id }).actions.setDashboardMode(
                                                  null,
                                                  DashboardEventSource.DashboardsList
                                              )
                                          }}
                                          fullWidth
                                      >
                                          View
                                      </LemonButton>
                                      <LemonButton
                                          to={urls.dashboard(id)}
                                          onClick={() => {
                                              dashboardLogic({ id }).mount()
                                              dashboardLogic({ id }).actions.setDashboardMode(
                                                  DashboardMode.Edit,
                                                  DashboardEventSource.DashboardsList
                                              )
                                          }}
                                          fullWidth
                                      >
                                          Edit
                                      </LemonButton>
                                      <LemonButton
                                          onClick={() => {
                                              showDuplicateDashboardModal(id, name)
                                          }}
                                          fullWidth
                                      >
                                          Duplicate
                                      </LemonButton>
                                      <LemonDivider />
                                      <LemonRow
                                          icon={<IconCottage className="text-warning" />}
                                          fullWidth
                                          status="warning"
                                      >
                                          <span className="text-muted">
                                              Change the default dashboard
                                              <br />
                                              from the <Link to={urls.projectHomepage()}>project home page</Link>.
                                          </span>
                                      </LemonRow>

                                      <LemonDivider />
                                      <LemonButton
                                          onClick={() => {
                                              showDeleteDashboardModal(id)
                                          }}
                                          fullWidth
                                          status="danger"
                                      >
                                          Delete dashboard
                                      </LemonButton>
                                  </>
                              }
                          />
                      )
                  },
              },
    ]

    return (
        <>
            <div className="flex justify-between gap-2 flex-wrap mb-4">
                <LemonInput
                    type="search"
                    placeholder="Search for dashboards"
                    onChange={(x) => setFilters({ search: x })}
                    value={filters.search}
                />
                <div className="flex items-center gap-4 flex-wrap">
                    <div className="flex items-center gap-2">
                        <span>Filter to:</span>
                        <div className="flex items-center gap-2">
                            <LemonButton
                                active={filters.pinned}
                                type="secondary"
                                size="small"
                                onClick={() => setFilters({ pinned: !filters.pinned })}
                                icon={<IconPin />}
                            >
                                Pinned
                            </LemonButton>
                        </div>
                        <div className="flex items-center gap-2">
                            <LemonButton
                                active={filters.shared}
                                type="secondary"
                                size="small"
                                onClick={() => setFilters({ shared: !filters.shared })}
                                icon={<IconShare />}
                            >
                                Shared
                            </LemonButton>
                        </div>
                    </div>
                    <div className="flex items-center gap-2">
                        <span>Created by:</span>
                        <MemberSelect
                            value={filters.createdBy === 'All users' ? null : filters.createdBy}
                            onChange={(user) => setFilters({ createdBy: user?.uuid || 'All users' })}
                        />
                    </div>
                    {extraActions}
                </div>
            </div>
            <LemonTable
                data-attr="dashboards-table"
                pagination={{ pageSize: 100 }}
                dataSource={dashboards as DashboardType[]}
                rowKey="id"
                rowClassName={(record) => (record._highlight ? 'highlighted' : null)}
                columns={columns}
                loading={dashboardsLoading}
                defaultSorting={{ columnKey: 'name', order: 1 }}
                emptyState="No dashboards matching your filters!"
                nouns={['dashboard', 'dashboards']}
            />
        </>
    )
}
