import { Meta } from '@storybook/react'
import { Billing } from './Billing'
import { useStorybookMocks, mswDecorator } from '~/mocks/browser'
import preflightJson from '~/mocks/fixtures/_preflight.json'
import billingJson from '~/mocks/fixtures/_billing.json'

export default {
    title: 'Scenes-Other/Billing',
    parameters: {
        layout: 'fullscreen',
        options: { showPanel: false },
        viewMode: 'story',
        testOptions: { skip: true },
    },
    decorators: [
        mswDecorator({
            get: {
                '/_preflight': {
                    ...preflightJson,
                    cloud: true,
                    realm: 'cloud',
                },
            },
        }),
    ],
} as Meta

export const _Billing = (): JSX.Element => {
    useStorybookMocks({
        get: {
            '/api/billing-v2/': {
                ...billingJson,
            },
        },
    })

    return <Billing />
}
